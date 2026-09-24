/** Owner-bound state for PRD-144 Workspace subscription and Internal FinOps. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";
import { workspaceNavigationOwner } from "../workspace-navigation/workspaceNavigationStore.js";

const NAVIGATION_PERMISSIONS = Object.freeze({
  "workspace-subscription": PERMISSIONS.WORKSPACE_COMMERCIAL_SUBSCRIPTION_READ,
  "billing-disputes": PERMISSIONS.WORKSPACE_COMMERCIAL_SUBSCRIPTION_READ,
  "internal-finops": PERMISSIONS.WORKSPACE_PROVIDER_COST_READ,
  "model-prices": PERMISSIONS.WORKSPACE_PROVIDER_COST_READ,
});

export function createWorkspaceCommercialAccountingStore({ onChange = () => {} } = {}) {
  let epoch = 0;
  let owner = "";
  const requests = new Map();
  let state = initialState();

  function publish() { onChange(snapshot()); }

  function prepare(navigation) {
    abortAll();
    epoch += 1;
    owner = "";
    state = { ...initialState(), navigation: safeNavigation(navigation), phase: "loading" };
    publish();
  }

  function activationFailed(navigation, error) {
    abortAll();
    epoch += 1;
    owner = "";
    const status = Number(error?.status) || 0;
    state = {
      ...initialState(),
      navigation: safeNavigation(navigation),
      phase: status === 401 ? "session_invalid"
        : status === 403 ? "permission_revoked"
          : status === 0 || status === 503 ? "dependency_unavailable" : "error",
      error: safeError(error),
    };
    publish();
  }

  function enter({ authContext, workspaceIdentity, workspaceSession, activeNavigationItemId }) {
    const signature = workspaceCommercialAccountingOwner({
      authContext,
      workspaceIdentity,
      workspaceSession,
      activeNavigationItemId,
    });
    if (!signature) {
      leave();
      return false;
    }
    if (signature !== owner) {
      abortAll();
      epoch += 1;
      owner = signature;
      const context = workspaceSession.context;
      state = {
        ...initialState(),
        navigation: activeNavigationItemId,
        workspaceId: String(context.workspace_id),
        workspaceRole: String(context.role),
        canSubmitDispute: hasPermission(
          authContext,
          PERMISSIONS.WORKSPACE_COMMERCIAL_DISPUTE_SUBMIT,
        ),
        canManageRates: hasPermission(authContext, PERMISSIONS.WORKSPACE_PROVIDER_COST_MANAGE),
      };
      publish();
    }
    return true;
  }

  function leave() {
    const changed = Boolean(owner || requests.size || hasFacts(state));
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
    state = {
      ...state,
      loading: Object.freeze([...new Set([...state.loading, name])]),
      error: null,
      phase: hasData(state) ? state.phase : "loading",
    };
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
    state = {
      ...state,
      ...(update ?? {}),
      loading: Object.freeze(state.loading.filter((lane) => lane !== ticket.lane)),
      error: null,
    };
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
        navigation: state.navigation,
        phase: status === 401 ? "session_invalid" : "permission_revoked",
        error: safeError(error),
      };
    } else {
      state = {
        ...state,
        loading: Object.freeze(state.loading.filter((lane) => lane !== ticket.lane)),
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

  function configureRates(filters = {}) {
    abortLane("rates");
    state = {
      ...state,
      rateFilters: Object.freeze({ ...filters }),
      rateCursor: null,
      rateHistory: Object.freeze([]),
      rates: null,
      error: null,
      feedback: "",
    };
    publish();
  }

  function moveRatePage(forward) {
    const history = [...state.rateHistory];
    if (forward) {
      const next = state.rates?.next_cursor;
      if (!next) return false;
      history.push(state.rateCursor);
      state = {
        ...state,
        rateCursor: next,
        rateHistory: Object.freeze(history),
        rates: null,
      };
    } else {
      if (!history.length) return false;
      state = {
        ...state,
        rateCursor: history.pop() ?? null,
        rateHistory: Object.freeze(history),
        rates: null,
      };
    }
    publish();
    return true;
  }

  function setIntent(kind, intent) {
    if (!Object.hasOwn(state.intents, kind)) return false;
    state = {
      ...state,
      intents: Object.freeze({
        ...state.intents,
        [kind]: intent ? Object.freeze({ ...intent }) : null,
      }),
    };
    publish();
    return true;
  }

  function setDraft(kind, draft) {
    if (!Object.hasOwn(state.drafts, kind)) return false;
    state = {
      ...state,
      drafts: Object.freeze({
        ...state.drafts,
        [kind]: Object.freeze({ ...(draft ?? {}) }),
      }),
    };
    publish();
    return true;
  }

  function setFeedback(value) {
    state = { ...state, feedback: safeText(value, ""), error: null };
    publish();
  }

  function snapshot() {
    return Object.freeze({
      ...state,
      loading: Object.freeze([...state.loading]),
      rateFilters: Object.freeze({ ...state.rateFilters }),
      rateHistory: Object.freeze([...state.rateHistory]),
      intents: Object.freeze({ ...state.intents }),
      drafts: Object.freeze({
        dispute: Object.freeze({ ...state.drafts.dispute }),
        rate: Object.freeze({ ...state.drafts.rate }),
      }),
    });
  }

  function abortLane(lane) {
    requests.get(lane)?.controller.abort();
    requests.delete(lane);
    state.loading = Object.freeze(state.loading.filter((item) => item !== lane));
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
    configureRates,
    nextRatePage: () => moveRatePage(true),
    previousRatePage: () => moveRatePage(false),
    setIntent,
    setDraft,
    setFeedback,
    snapshot,
  });
}

export function workspaceCommercialAccountingOwner({
  authContext,
  workspaceIdentity,
  workspaceSession,
  activeNavigationItemId,
} = {}) {
  const navigation = safeNavigation(activeNavigationItemId);
  const requiredPermission = NAVIGATION_PERMISSIONS[navigation];
  if (workspaceIdentity !== undefined) {
    const owner = workspaceNavigationOwner(workspaceIdentity, workspaceSession);
    if (!owner || !requiredPermission || !hasPermission(authContext, requiredPermission)
      || owner.permissionRevision !== authContext?.permission_revision
      || owner.membershipRevision !== authContext?.membership_revision
      || owner.permissionFingerprint !== authContext?.permission_fingerprint
      || Date.parse(owner.globalExpiresAt) <= Date.now()
      || Date.parse(owner.workspaceExpiresAt) <= Date.now()) return "";
    return JSON.stringify({ ...owner, navigation, requiredPermission });
  }
  const identity = authorizationIdentitySnapshot(authContext, requiredPermission);
  const context = workspaceSession?.context;
  const sessionId = String(
    context?.workspace_session_id
      ?? workspaceSession?.workspace_session_id
      ?? authContext?.session?.session_id
      ?? "",
  );
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  if (!identity || !requiredPermission
    || !/^wsp_[0-9a-f]{32}$/u.test(String(context?.workspace_id ?? ""))
    || context?.session_status !== "active"
    || !String(context?.role ?? "")
    || !sessionId || !revision || !fingerprint) return "";
  return JSON.stringify({
    identity,
    workspace_session_id: sessionId,
    workspace_id: context.workspace_id,
    membership_role: context.role,
    permission_revision: revision,
    permission_fingerprint: fingerprint,
    expires_at: context.expires_at,
    navigation,
  });
}

function initialState() {
  return {
    phase: "idle",
    loading: Object.freeze([]),
    navigation: "",
    workspaceId: "",
    workspaceRole: "",
    canSubmitDispute: false,
    canManageRates: false,
    permissionRevision: null,
    permissionFingerprint: "",
    subscription: null,
    rateFilters: Object.freeze({
      provider_id: "", model_id: "", currency: "", status: "", effective_at: "",
    }),
    rateCursor: null,
    rateHistory: Object.freeze([]),
    rates: null,
    feedback: "",
    error: null,
    intents: Object.freeze({ dispute: null, rate: null }),
    drafts: Object.freeze({ dispute: Object.freeze({}), rate: Object.freeze({}) }),
  };
}

function derivePhase(value) {
  if (value.error) return value.phase;
  if (["workspace-subscription", "billing-disputes"].includes(value.navigation)) {
    if (!value.subscription) return value.loading.length ? "loading" : "idle";
    const projection = value.subscription.subscription;
    if (!projection) return "empty";
    if (projection.projection_state === "reconciliation_required") {
      return "reconciliation_required";
    }
    if (projection.projection_state === "partial") return "partial";
    if (["stale", "unknown"].includes(projection.projection_state)) return "stale";
    if (String(projection.payment_status).includes("pending")) return "payment_pending";
    if (projection.invoice_status === "failed") return "invoice_failed";
    if (String(projection.subscription_status).includes("recover")) {
      return "recovery_in_progress";
    }
    return "ready";
  }
  if (["internal-finops", "model-prices"].includes(value.navigation)) {
    if (!value.rates) return value.loading.length ? "loading" : "idle";
    return value.rates.rates.length ? "ready" : "empty";
  }
  return "idle";
}

function hasData(value) { return Boolean(value.subscription || value.rates); }
function hasFacts(value) {
  return Boolean(
    hasData(value) || value.error || value.feedback || value.loading.length
      || value.intents.dispute || value.intents.rate
      || Object.keys(value.drafts.dispute).length || Object.keys(value.drafts.rate).length,
  );
}
function safeError(error) {
  return Object.freeze({
    status: Number(error?.status) || 0,
    code: safeText(error?.code, "workspace.request_failed"),
    message: safeText(error?.message, "Workspace 商业账务请求未完成。"),
    requestId: safeText(error?.requestId, "-"),
  });
}
function safeText(value, fallback) {
  if (typeof value !== "string" || !value) return fallback;
  return value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240);
}
function safeNavigation(value) {
  const navigation = String(value ?? "");
  return Object.hasOwn(NAVIGATION_PERMISSIONS, navigation) ? navigation : "";
}
function laneName(value) {
  const lane = String(value ?? "");
  if (!/^[a-z][a-z-]{1,31}$/u.test(lane)) throw new TypeError("invalid request lane");
  return lane;
}
