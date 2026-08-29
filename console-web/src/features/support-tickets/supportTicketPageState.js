/** PRD-132 owner-bound Workspace support ticket state. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

const PAGE_ID = "support-tickets";
const LANES = Object.freeze(["list", "mutation"]);
const SAFE_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u;
const SAFE_CHAIN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;

export function supportTicketReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.SUPPORT_TICKET_READ);
}

export function supportTicketPageOwner(authContext, consoleShellState) {
  const readIdentity = supportTicketReadIdentitySnapshot(authContext);
  const permissions = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const required = normalizePermissions(item?.required_permissions);
  const navigationPermissions = normalizePermissions(navigation?.permission_codes);
  if (
    !readIdentity
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.SUPPORT_TICKET_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page: PAGE_ID,
    tenantId: String(authContext?.user?.tenant_id ?? authContext?.tenant?.tenant_id ?? ""),
    accountId: String(authContext?.user?.account_id ?? authContext?.user?.user_id ?? ""),
    readIdentity,
    createIdentity: hasPermission(authContext, PERMISSIONS.SUPPORT_TICKET_CREATE)
      ? authorizationIdentitySnapshot(authContext, PERMISSIONS.SUPPORT_TICKET_CREATE) : "",
    manageIdentity: hasPermission(authContext, PERMISSIONS.SUPPORT_TICKET_MANAGE)
      ? authorizationIdentitySnapshot(authContext, PERMISSIONS.SUPPORT_TICKET_MANAGE) : "",
    navigationOwner: JSON.stringify([
      item.id, item.target_section, required, permissions, revision, fingerprint,
    ]),
    canCreate: hasPermission(authContext, PERMISSIONS.SUPPORT_TICKET_CREATE),
    canManage: hasPermission(authContext, PERMISSIONS.SUPPORT_TICKET_MANAGE),
  });
}

export function createSupportTicketPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let pageGeneration = 0;
  let state = emptyState();
  const intentKeys = new Map();
  const lanes = new Map(LANES.map((name) => [name, {
    generation: 0, controller: null, ticket: null,
  }]));

  const identityKey = (owner) => owner ? JSON.stringify([
    owner.readIdentity, owner.createIdentity, owner.manageIdentity, owner.navigationOwner,
  ]) : "";

  const abortLane = (name) => {
    const lane = lanes.get(name);
    lane?.controller?.abort();
    if (lane) { lane.controller = null; lane.ticket = null; }
  };
  const abortAll = () => LANES.forEach(abortLane);
  const clearInternal = (nextOwnerKey = "") => {
    clearDom();
    abortAll();
    intentKeys.clear();
    state = emptyState();
    ownerKey = nextOwnerKey;
    pageGeneration += 1;
  };
  const isCurrent = (ticket, owner) => {
    const lane = lanes.get(ticket?.lane);
    return Boolean(ticket && lane?.ticket === ticket && ticket.ownerKey === ownerKey
      && ticket.pageGeneration === pageGeneration && ticket.generation === lane.generation
      && identityKey(owner) === ownerKey && !ticket.signal.aborted);
  };
  const begin = (laneName, owner, details = {}) => {
    const lane = lanes.get(laneName);
    if (!lane || !ownerKey || identityKey(owner) !== ownerKey) return null;
    abortLane(laneName);
    lane.generation += 1;
    lane.controller = new AbortController();
    lane.ticket = Object.freeze({
      lane: laneName,
      generation: lane.generation,
      pageGeneration,
      ownerKey,
      controller: lane.controller,
      signal: lane.controller.signal,
      ...details,
    });
    state = freezeState({ ...state, busy: { ...state.busy, [laneName]: true }, feedback: null });
    return lane.ticket;
  };

  return {
    enter(owner) {
      const next = identityKey(owner);
      if (next !== ownerKey) clearInternal(next);
      return Boolean(ownerKey);
    },
    snapshot() { return state; },
    setFilters(owner, filters = {}) {
      if (identityKey(owner) !== ownerKey) return false;
      const status = String(filters.status ?? "");
      const severity = String(filters.severity ?? "");
      if (!["", "open", "triaged", "waiting_customer", "waiting_platform", "resolved", "closed"].includes(status)
        || !["", "p1", "p2", "p3", "p4"].includes(severity)) return false;
      state = freezeState({ ...state, filters: { status, severity } });
      return true;
    },
    select(owner, ticketId) {
      if (identityKey(owner) !== ownerKey
        || !state.items.some((item) => item.ticket_id === ticketId)) return false;
      state = freezeState({ ...state, selectedTicketId: ticketId, feedback: null });
      return true;
    },
    beginList(owner) { return begin("list", owner); },
    acceptList(operation, response, owner) {
      if (!isCurrent(operation, owner) || response?.actor_domain !== "workspace"
        || !Array.isArray(response.items)) return false;
      const selected = response.items.some((item) => item.ticket_id === state.selectedTicketId)
        ? state.selectedTicketId : response.items[0]?.ticket_id ?? null;
      state = freezeState({
        ...state,
        items: response.items,
        selectedTicketId: selected,
        requestId: safeChain(response.request_id),
      });
      return true;
    },
    idempotencyKey(owner, scope, intent, createKey) {
      if (identityKey(owner) !== ownerKey || typeof createKey !== "function") return "";
      const signature = JSON.stringify(intent);
      const current = intentKeys.get(scope);
      if (current?.signature === signature) return current.key;
      const key = createKey();
      intentKeys.set(scope, Object.freeze({ signature, key }));
      return key;
    },
    completeIntent(owner, scope) {
      return identityKey(owner) === ownerKey && intentKeys.delete(scope);
    },
    beginMutation(owner, details = {}) {
      if (!owner?.canCreate && !owner?.canManage) return null;
      return begin("mutation", owner, details);
    },
    acceptTicket(operation, response, owner, successCode) {
      if (!isCurrent(operation, owner) || response?.actor_domain !== "workspace"
        || !response.ticket) return false;
      state = withUpsertedTicket(state, response.ticket, {
        kind: "success", code: successCode, requestId: safeChain(response.request_id),
      });
      return true;
    },
    acceptAttachment(operation, response, owner) {
      if (!isCurrent(operation, owner) || !response?.attachment) return false;
      const current = state.items.find((item) => item.ticket_id === operation.ticketId);
      if (!current) return false;
      const attachments = [
        ...current.attachments.filter((item) => item.attachment_id !== response.attachment.attachment_id),
        response.attachment,
      ];
      state = withUpsertedTicket(state, {
        ...current, revision: response.ticket_revision, attachments,
      }, {
        kind: "success", code: "support.attachment_quarantined", requestId: safeChain(response.request_id),
      });
      return true;
    },
    acceptEscalation(operation, response, owner) {
      if (!isCurrent(operation, owner) || !response?.escalation) return false;
      const current = state.items.find((item) => item.ticket_id === operation.ticketId);
      if (!current) return false;
      const escalations = [
        ...current.escalations.filter((item) => item.escalation_id !== response.escalation.escalation_id),
        response.escalation,
      ];
      state = withUpsertedTicket(state, {
        ...current, revision: response.ticket_revision, escalations,
      }, {
        kind: "success", code: "support.ticket_escalated", requestId: safeChain(response.request_id),
      });
      return true;
    },
    reject(operation, error, owner) {
      if (!isCurrent(operation, owner)) return false;
      state = freezeState({
        ...state,
        feedback: {
          kind: "error",
          code: SAFE_CODE.test(String(error?.code ?? "")) ? String(error.code) : "support.unavailable",
          requestId: safeChain(error?.requestId),
          status: Number.isSafeInteger(Number(error?.status)) ? Number(error.status) : 0,
        },
      });
      return true;
    },
    finish(operation) {
      const lane = lanes.get(operation?.lane);
      if (lane?.ticket !== operation) return false;
      lane.ticket = null;
      lane.controller = null;
      state = freezeState({ ...state, busy: { ...state.busy, [operation.lane]: false } });
      return true;
    },
    blocksShellDomReplacement() { return Object.values(state.busy).some(Boolean); },
    isCurrent,
    clear() { clearInternal(""); },
  };
}

export function isSupportTicketAbortError(error) {
  return error?.name === "AbortError" || error?.code === "ABORT_ERR";
}

function withUpsertedTicket(state, ticket, feedback) {
  const items = [...state.items];
  const index = items.findIndex((item) => item.ticket_id === ticket.ticket_id);
  if (index < 0) items.unshift(ticket); else items[index] = ticket;
  return freezeState({ ...state, items, selectedTicketId: ticket.ticket_id, feedback });
}
function emptyState() {
  return freezeState({
    items: [], selectedTicketId: null, filters: { status: "", severity: "" },
    feedback: null, requestId: "-", busy: { list: false, mutation: false },
  });
}
function freezeState(value) {
  return Object.freeze({
    ...value,
    items: Object.freeze([...(value.items ?? [])]),
    filters: Object.freeze({ ...(value.filters ?? {}) }),
    busy: Object.freeze({ ...(value.busy ?? {}) }),
    feedback: value.feedback ? Object.freeze({ ...value.feedback }) : null,
  });
}
function normalizePermissions(value) {
  if (!Array.isArray(value)) return null;
  const normalized = value.map(String).sort();
  return new Set(normalized).size === normalized.length ? normalized : null;
}
function safeChain(value) {
  const text = String(value ?? "");
  return SAFE_CHAIN.test(text) ? text : "-";
}
