/** Owner-bound state for the PRD-144 Platform commercial accounting workbench. */

const READ_PERMISSIONS = new Set([
  "platform.commercial.plan.read",
  "platform.commercial.subscription.read",
]);

export function createPlatformCommercialAccountingStore({ onChange = () => {} } = {}) {
  let epoch = 0;
  let owner = "";
  const requests = new Map();
  let state = initialState();

  function publish() { onChange(snapshot()); }

  function enter(context, navigationItems) {
    const signature = platformCommercialAccountingOwner(context, navigationItems);
    if (!signature) {
      leave();
      return false;
    }
    if (signature !== owner) {
      abortAll();
      epoch += 1;
      owner = signature;
      state = initialState();
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
    if (status === 403) {
      abortAll();
      state = { ...initialState(), phase: "permission_revoked", error: safeError(error) };
    } else {
      state = {
        ...state,
        loading: Object.freeze(state.loading.filter((lane) => lane !== ticket.lane)),
        error: safeError(error),
        phase: status === 409 || status === 412
          ? "revision_conflict"
          : status === 0 || status === 503
            ? "dependency_unavailable"
            : "error",
      };
    }
    publish();
    return true;
  }

  function configure(kind, filters = {}) {
    const config = pageConfig(kind);
    if (!config) return false;
    abortLane(config.lane);
    state = {
      ...state,
      [config.filters]: Object.freeze({ ...filters }),
      [config.cursor]: null,
      [config.history]: Object.freeze([]),
      [config.response]: null,
      error: null,
      feedback: "",
    };
    publish();
    return true;
  }

  function movePage(kind, forward) {
    const config = pageConfig(kind);
    if (!config) return false;
    const history = [...state[config.history]];
    if (forward) {
      const next = state[config.response]?.next_cursor;
      if (!next) return false;
      history.push(state[config.cursor]);
      state = {
        ...state,
        [config.cursor]: next,
        [config.history]: Object.freeze(history),
        [config.response]: null,
      };
    } else {
      if (!history.length) return false;
      state = {
        ...state,
        [config.cursor]: history.pop() ?? null,
        [config.history]: Object.freeze(history),
        [config.response]: null,
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

  function setFeedback(value) {
    state = { ...state, feedback: safeText(value, ""), error: null };
    publish();
  }

  function snapshot() {
    return Object.freeze({
      ...state,
      loading: Object.freeze([...state.loading]),
      planFilters: Object.freeze({ ...state.planFilters }),
      subscriptionFilters: Object.freeze({ ...state.subscriptionFilters }),
      planHistory: Object.freeze([...state.planHistory]),
      subscriptionHistory: Object.freeze([...state.subscriptionHistory]),
      intents: Object.freeze({ ...state.intents }),
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
    enter,
    leave,
    begin,
    isCurrent,
    accept,
    reject,
    configure,
    nextPage: (kind) => movePage(kind, true),
    previousPage: (kind) => movePage(kind, false),
    setIntent,
    setFeedback,
    snapshot,
  });
}

export function platformCommercialAccountingOwner(context, navigationItems) {
  const operator = context?.operator;
  const permissions = Array.isArray(context?.permissions) ? [...context.permissions].sort() : [];
  const navigation = Array.isArray(navigationItems) ? navigationItems : [];
  if (!navigation.some((item) => item?.id === "commercial-accounting")
    || !/^pto_[0-9a-f]{32}$/u.test(String(operator?.operator_id ?? ""))
    || !Number.isSafeInteger(operator?.revision) || operator.revision < 1
    || !permissions.some((permission) => READ_PERMISSIONS.has(permission))) return "";
  return JSON.stringify({
    operator_id: operator.operator_id,
    operator_revision: operator.revision,
    roles: Array.isArray(operator.roles) ? [...operator.roles].sort() : [],
    permissions,
    expires_at: String(context.expires_at ?? ""),
    navigation: "commercial-accounting",
  });
}

function initialState() {
  return {
    phase: "idle",
    loading: Object.freeze([]),
    planFilters: Object.freeze({
      plan_code: "", status: "", currency: "", billing_period: "",
    }),
    planCursor: null,
    planHistory: Object.freeze([]),
    plans: null,
    subscriptionFilters: Object.freeze({
      status: "", plan_catalog_id: "", projection_state: "", from: "", until: "",
    }),
    subscriptionCursor: null,
    subscriptionHistory: Object.freeze([]),
    subscriptions: null,
    feedback: "",
    error: null,
    intents: Object.freeze({ plan: null, reference: null, dispute: null }),
  };
}

function pageConfig(kind) {
  return {
    plans: {
      lane: "plans",
      filters: "planFilters",
      cursor: "planCursor",
      history: "planHistory",
      response: "plans",
    },
    subscriptions: {
      lane: "subscriptions",
      filters: "subscriptionFilters",
      cursor: "subscriptionCursor",
      history: "subscriptionHistory",
      response: "subscriptions",
    },
  }[kind] ?? null;
}

function derivePhase(value) {
  if (value.error) return value.phase;
  if (!hasData(value)) return value.loading.length ? "loading" : "idle";
  const subscriptions = value.subscriptions?.subscriptions ?? [];
  if (subscriptions.some((item) => item.projection_state === "reconciliation_required")) {
    return "reconciliation_required";
  }
  if (subscriptions.some((item) => item.projection_state === "partial")) return "partial";
  if (subscriptions.some((item) => ["stale", "unknown"].includes(item.projection_state))) {
    return "stale";
  }
  if (!(value.plans?.plans.length || subscriptions.length)) return "empty";
  return "ready";
}

function hasData(value) { return Boolean(value.plans || value.subscriptions); }
function hasFacts(value) {
  return Boolean(hasData(value) || value.error || value.feedback || value.loading.length);
}
function safeError(error) {
  return Object.freeze({
    status: Number(error?.status) || 0,
    code: safeText(error?.code, "platform.request_failed"),
    message: safeText(error?.message, "商业账务请求未完成。"),
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
