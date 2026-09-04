/** Workspace-owned state for PRD-143 Provider compatibility and error overrides. */

import {
  authorizationIdentitySnapshot,
  hasPermission,
  PERMISSIONS,
} from "../../lib/permissions.js";

const READ_ROLES = new Set(["owner", "admin", "developer", "security_admin"]);
const NAVIGATION_IDS = new Set(["providers", "error-maps"]);

export function createWorkspaceProviderEngineeringStore({ onChange = () => {} } = {}) {
  let epoch = 0;
  let owner = "";
  const requests = new Map();
  let state = initialState();

  function publish() { onChange(snapshot()); }

  function prepare(providerId, activeNavigationItemId = "") {
    abortAll();
    epoch += 1;
    owner = "";
    state = {
      ...initialState(),
      activeNavigationItemId: String(activeNavigationItemId),
      providerId: String(providerId ?? ""),
      phase: "loading",
    };
    publish();
  }

  function activationFailed(providerId, error, activeNavigationItemId = "") {
    abortAll();
    epoch += 1;
    owner = "";
    const status = Number(error?.status) || 0;
    state = {
      ...initialState(),
      activeNavigationItemId: String(activeNavigationItemId),
      providerId: String(providerId ?? ""),
      phase: status === 401 ? "session_invalid"
        : status === 403 ? "permission_revoked"
          : status === 0 || status === 503 ? "dependency_unavailable" : "error",
      error: safeError(error),
    };
    publish();
  }

  function enter({ authContext, workspaceSession, activeNavigationItemId, providerId }) {
    const signature = workspaceProviderEngineeringOwner({
      authContext,
      workspaceSession,
      activeNavigationItemId,
      providerId,
    });
    if (!signature) {
      leave();
      return false;
    }
    if (signature !== owner) {
      abortAll();
      epoch += 1;
      owner = signature;
      state = {
        ...initialState(),
        activeNavigationItemId: String(activeNavigationItemId),
        providerId: String(providerId),
        workspaceId: String(workspaceSession.context.workspace_id),
        canManage: hasPermission(
          authContext,
          PERMISSIONS.WORKSPACE_PROVIDER_ERROR_OVERRIDE_MANAGE,
        ),
      };
      publish();
    }
    return true;
  }

  function leave() {
    const changed = Boolean(owner || state.providerId || requests.size || state.response || state.error);
    abortAll();
    epoch += 1;
    owner = "";
    state = initialState();
    if (changed) publish();
  }

  function begin(lane) {
    const name = laneName(lane);
    requests.get(name)?.controller.abort();
    const ticket = Object.freeze({
      lane: name,
      epoch,
      owner,
      controller: new AbortController(),
    });
    requests.set(name, ticket);
    state = { ...state, loading: true, error: null, phase: state.response ? state.phase : "loading" };
    publish();
    return ticket;
  }

  function isCurrent(ticket) {
    return Boolean(
      ticket
      && requests.get(ticket.lane) === ticket
      && ticket.epoch === epoch
      && ticket.owner === owner
      && owner
      && !ticket.controller.signal.aborted,
    );
  }

  function accept(ticket, patch) {
    if (!isCurrent(ticket)) return false;
    requests.delete(ticket.lane);
    const update = typeof patch === "function" ? patch(state) : patch;
    state = { ...state, ...(update ?? {}), loading: requests.size > 0, error: null };
    state.phase = derivePhase(state);
    publish();
    return true;
  }

  function reject(ticket, error) {
    if (!isCurrent(ticket) || error?.name === "AbortError") return false;
    requests.delete(ticket.lane);
    const status = Number(error?.status) || 0;
    if (status === 401 || status === 403) {
      abortAll();
      state = {
        ...initialState(),
        activeNavigationItemId: state.activeNavigationItemId,
        providerId: state.providerId,
        phase: status === 403 ? "permission_revoked" : "session_invalid",
        error: safeError(error),
      };
    } else {
      state = {
        ...state,
        loading: requests.size > 0,
        phase: status === 409 || status === 412
          ? "revision_conflict"
          : status === 0 || status === 503
            ? "dependency_unavailable"
            : "error",
        error: safeError(error),
      };
    }
    publish();
    return true;
  }

  function configure(filters = {}) {
    abortAll();
    state = {
      ...state,
      filters: Object.freeze({ ...filters }),
      cursor: null,
      history: Object.freeze([]),
      response: null,
      error: null,
      phase: "idle",
    };
    publish();
  }

  function movePage(forward) {
    const history = [...state.history];
    if (forward) {
      const next = state.response?.next_cursor;
      if (!next) return false;
      history.push(state.cursor);
      state = { ...state, cursor: next, history: Object.freeze(history), response: null };
    } else {
      if (!history.length) return false;
      state = { ...state, cursor: history.pop() ?? null, history: Object.freeze(history), response: null };
    }
    publish();
    return true;
  }

  function setIntent(intent) {
    state = { ...state, intent: intent ? Object.freeze({ ...intent }) : null };
    publish();
  }

  function setFeedback(value) {
    state = { ...state, feedback: safeText(value, ""), error: null };
    publish();
  }

  function snapshot() {
    return Object.freeze({
      ...state,
      filters: Object.freeze({ ...state.filters }),
      history: Object.freeze([...state.history]),
      intent: state.intent ? Object.freeze({ ...state.intent }) : null,
    });
  }

  function abortAll() {
    for (const ticket of requests.values()) ticket.controller.abort();
    requests.clear();
  }

  return Object.freeze({
    prepare,
    activationFailed,
    enter,
    leave,
    begin,
    isCurrent,
    accept,
    reject,
    configure,
    nextPage: () => movePage(true),
    previousPage: () => movePage(false),
    setIntent,
    setFeedback,
    snapshot,
  });
}

export function workspaceProviderEngineeringOwner({
  authContext,
  workspaceSession,
  activeNavigationItemId,
  providerId,
} = {}) {
  const overrideIdentity = authorizationIdentitySnapshot(
    authContext,
    PERMISSIONS.WORKSPACE_PROVIDER_ERROR_OVERRIDE_READ,
  );
  const providerIdentity = activeNavigationItemId === "providers"
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.PROVIDER_READ)
    : "";
  const context = workspaceSession?.context;
  const id = String(providerId ?? "");
  if (!overrideIdentity
    || !NAVIGATION_IDS.has(activeNavigationItemId)
    || (activeNavigationItemId === "providers" && !providerIdentity)
    || !/^wsp_[0-9a-f]{32}$/u.test(String(context?.workspace_id ?? ""))
    || context?.session_status !== "active"
    || !READ_ROLES.has(String(context?.role ?? ""))
    || (id && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(id))
    || (activeNavigationItemId === "providers" && !id)) return "";
  return JSON.stringify({
    override_identity: overrideIdentity,
    provider_identity: providerIdentity,
    workspace_id: context.workspace_id,
    workspace_role: context.role,
    expires_at: context.expires_at,
    provider_id: id,
    navigation: activeNavigationItemId,
  });
}

function initialState() {
  return {
    phase: "idle",
    loading: false,
    activeNavigationItemId: "",
    providerId: "",
    workspaceId: "",
    canManage: false,
    filters: Object.freeze({ status: "", output_error_code: "" }),
    cursor: null,
    history: Object.freeze([]),
    response: null,
    feedback: "",
    error: null,
    intent: null,
  };
}

function derivePhase(value) {
  if (value.error) return value.phase;
  if (!value.response) return value.loading ? "loading" : "idle";
  if (!value.response.overrides.length) return "empty";
  const compatibility = value.response.compatibility?.compatibility_status;
  if (compatibility === "blocked") return "blocked";
  if (compatibility === "warning" || compatibility === "unknown") return "stale";
  return "ready";
}

function safeError(error) {
  return Object.freeze({
    status: Number(error?.status) || 0,
    code: safeText(error?.code, "workspace.request_failed"),
    message: safeText(error?.message, "Provider detail request did not complete."),
    requestId: safeText(error?.requestId, "-"),
  });
}

function safeText(value, fallback) {
  if (typeof value !== "string" || !value) return fallback;
  return value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240);
}

function laneName(value) {
  const lane = String(value ?? "");
  if (!/^[a-z][a-z-]{1,31}$/u.test(lane)) throw new TypeError("invalid request lane");
  return lane;
}
