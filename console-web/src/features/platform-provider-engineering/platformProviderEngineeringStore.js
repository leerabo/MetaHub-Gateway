/** Owner-bound state for the PRD-143 Platform Provider Engineering workbench. */

const READ_PERMISSIONS = new Set([
  "platform.provider_adapter.read",
  "platform.error_catalog.read",
]);

export function createPlatformProviderEngineeringStore({ onChange = () => {} } = {}) {
  let epoch = 0;
  let owner = "";
  const requests = new Map();
  let state = initialState();

  function publish() { onChange(snapshot()); }

  function enter(context, navigationItems) {
    const signature = platformProviderEngineeringOwner(context, navigationItems);
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
      generation: (requests.get(name)?.generation ?? 0) + 1,
      controller: new AbortController(),
    });
    requests.set(name, ticket);
    state = {
      ...state,
      loading: Object.freeze([...new Set([...state.loading, name])]),
      error: null,
      phase: state.adapters ? state.phase : "loading",
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
          : status === 503 || status === 0
            ? "dependency_unavailable"
            : "error",
      };
    }
    publish();
    return true;
  }

  function configureAdapters(filters = {}) {
    abortLanes(["adapters", "runs", "cases"]);
    state = {
      ...state,
      filters: Object.freeze({ ...filters }),
      adapterCursor: null,
      adapterHistory: Object.freeze([]),
      adapters: null,
      selectedAdapterId: null,
      runs: null,
      cases: null,
      selectedRunId: null,
      runCursor: null,
      runHistory: Object.freeze([]),
      caseCursor: null,
      caseHistory: Object.freeze([]),
      error: null,
    };
    publish();
  }

  function setRunFilters(filters = {}) {
    abortLanes(["runs", "cases"]);
    state = {
      ...state,
      runFilters: Object.freeze({ ...filters }),
      runCursor: null,
      runHistory: Object.freeze([]),
      runs: null,
      selectedRunId: null,
      caseCursor: null,
      caseHistory: Object.freeze([]),
      cases: null,
      error: null,
    };
    publish();
  }

  function selectAdapter(adapterId) {
    const id = String(adapterId ?? "");
    if (!state.adapters?.adapters.some((item) => item.provider_adapter_release_id === id)) {
      return false;
    }
    if (state.selectedAdapterId === id) return true;
    abortLanes(["runs", "cases"]);
    state = {
      ...state,
      selectedAdapterId: id,
      runs: null,
      cases: null,
      selectedRunId: null,
      runCursor: null,
      runHistory: Object.freeze([]),
      caseCursor: null,
      caseHistory: Object.freeze([]),
      error: null,
    };
    publish();
    return true;
  }

  function selectRun(runId) {
    const id = String(runId ?? "");
    if (!state.runs?.runs.some((item) => item.contract_run_id === id)) return false;
    if (state.selectedRunId === id) return true;
    abortLanes(["cases"]);
    state = {
      ...state,
      selectedRunId: id,
      cases: null,
      caseCursor: null,
      caseHistory: Object.freeze([]),
      error: null,
    };
    publish();
    return true;
  }

  function nextPage(kind) { return movePage(kind, true); }
  function previousPage(kind) { return movePage(kind, false); }

  function movePage(kind, forward) {
    const config = pageConfig(kind);
    if (!config) return false;
    const current = state[config.cursor];
    const history = [...state[config.history]];
    if (forward) {
      const next = state[config.response]?.next_cursor;
      if (!next) return false;
      history.push(current);
      state = { ...state, [config.cursor]: next, [config.history]: Object.freeze(history), [config.response]: null };
    } else {
      if (!history.length) return false;
      const previous = history.pop() ?? null;
      state = { ...state, [config.cursor]: previous, [config.history]: Object.freeze(history), [config.response]: null };
    }
    publish();
    return true;
  }

  function setIntent(kind, intent) {
    state = {
      ...state,
      intents: Object.freeze({ ...state.intents, [kind]: intent ? Object.freeze({ ...intent }) : null }),
    };
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
      runFilters: Object.freeze({ ...state.runFilters }),
      loading: Object.freeze([...state.loading]),
      adapterHistory: Object.freeze([...state.adapterHistory]),
      runHistory: Object.freeze([...state.runHistory]),
      caseHistory: Object.freeze([...state.caseHistory]),
      intents: Object.freeze({ ...state.intents }),
    });
  }

  function abortAll() {
    for (const ticket of requests.values()) ticket.controller.abort();
    requests.clear();
  }

  function abortLanes(lanes) {
    for (const lane of lanes) {
      requests.get(lane)?.controller.abort();
      requests.delete(lane);
    }
    state.loading = Object.freeze(state.loading.filter((lane) => !lanes.includes(lane)));
  }

  return Object.freeze({
    enter,
    leave,
    begin,
    isCurrent,
    accept,
    reject,
    configureAdapters,
    setRunFilters,
    selectAdapter,
    selectRun,
    nextPage,
    previousPage,
    setIntent,
    setFeedback,
    snapshot,
  });
}
export function platformProviderEngineeringOwner(context, navigationItems) {
  const operator = context?.operator;
  const permissions = Array.isArray(context?.permissions) ? [...context.permissions].sort() : [];
  const navigation = Array.isArray(navigationItems) ? navigationItems : [];
  if (!navigation.some((item) => item?.id === "provider-engineering")
    || !/^pto_[0-9a-f]{32}$/u.test(String(operator?.operator_id ?? ""))
    || !Number.isSafeInteger(operator?.revision) || operator.revision < 1
    || !permissions.some((permission) => READ_PERMISSIONS.has(permission))) return "";
  return JSON.stringify({
    operator_id: operator.operator_id,
    operator_revision: operator.revision,
    roles: Array.isArray(operator.roles) ? [...operator.roles].sort() : [],
    permissions,
    expires_at: String(context.expires_at ?? ""),
    navigation: "provider-engineering",
  });
}

function initialState() {
  return {
    phase: "idle",
    loading: Object.freeze([]),
    filters: Object.freeze({}),
    adapterCursor: null,
    adapterHistory: Object.freeze([]),
    adapters: null,
    selectedAdapterId: null,
    runFilters: Object.freeze({}),
    runCursor: null,
    runHistory: Object.freeze([]),
    runs: null,
    selectedRunId: null,
    caseCursor: null,
    caseHistory: Object.freeze([]),
    cases: null,
    catalog: null,
    feedback: "",
    error: null,
    intents: Object.freeze({ adapter: null, catalog: null }),
  };
}

function derivePhase(value) {
  if (value.error) return value.phase;
  if (value.adapters === null) return value.loading.includes("adapters") ? "loading" : "idle";
  if (!value.adapters.adapters.length) return "empty";
  if (value.adapters.adapters.some((item) => ["testing", "review_required"].includes(item.status))) return "testing";
  return "ready";
}

function pageConfig(kind) {
  return {
    adapters: { cursor: "adapterCursor", history: "adapterHistory", response: "adapters" },
    runs: { cursor: "runCursor", history: "runHistory", response: "runs" },
    cases: { cursor: "caseCursor", history: "caseHistory", response: "cases" },
  }[kind] ?? null;
}

function hasFacts(value) {
  return Boolean(
    value.adapters || value.runs || value.cases || value.catalog || value.error
    || value.feedback || value.loading.length,
  );
}

function safeError(error) {
  return Object.freeze({
    status: Number(error?.status) || 0,
    code: safeText(error?.code, "platform.request_failed"),
    message: safeText(error?.message, "Provider Engineering 请求未完成。"),
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
