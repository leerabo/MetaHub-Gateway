/** Owner-bound state for the PRD-141 Platform Growth workbench. */

const READ_PERMISSIONS = new Set([
  "platform.growth.read",
  "platform.growth.adoption_read",
  "platform.growth.channel_attribution_read",
]);
const DATA_TABS = new Set(["funnels", "cohorts", "campaigns", "experiments", "attribution"]);

export function createPlatformGrowthStore({ onChange = () => {} } = {}) {
  let epoch = 0;
  let owner = "";
  const requests = new Map();
  let state = initialState([]);

  function publish() {
    onChange(snapshot());
  }

  function enter(context, navigationItems) {
    const signature = ownerSignature(context, navigationItems);
    if (!signature) {
      leave();
      return false;
    }
    if (signature !== owner) {
      clearRequests();
      epoch += 1;
      owner = signature;
      state = initialState(context.permissions ?? []);
      publish();
    }
    return true;
  }

  function leave() {
    const changed = owner || requests.size || hasFacts(state);
    clearRequests();
    epoch += 1;
    owner = "";
    state = initialState([]);
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
    };
    state.phase = derivePhase(state);
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

  function accept(ticket, update) {
    if (!isCurrent(ticket)) return false;
    requests.delete(ticket.lane);
    const patch = typeof update === "function" ? update(state) : update;
    state = {
      ...state,
      ...(patch ?? {}),
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
      clearRequests();
      state = { ...initialState([]), phase: "permission_revoked", error: safeError(error) };
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

  function selectTab(tab) {
    const value = String(tab ?? "");
    if (!state.availableTabs.includes(value)) return false;
    state = { ...state, tab: value, error: null, feedback: "" };
    state.phase = derivePhase(state);
    publish();
    return true;
  }

  function configure(tab, filters = {}) {
    const name = dataTab(tab);
    if (!state.availableTabs.includes(name)) return false;
    state = {
      ...state,
      filters: Object.freeze({ ...state.filters, [name]: Object.freeze({ ...filters }) }),
      cursors: Object.freeze({ ...state.cursors, [name]: null }),
      histories: Object.freeze({ ...state.histories, [name]: Object.freeze([]) }),
      data: Object.freeze({ ...state.data, [name]: null }),
      error: null,
      feedback: "",
    };
    state.phase = derivePhase(state);
    publish();
    return true;
  }

  function nextPage(tab) {
    const name = dataTab(tab);
    const next = state.data[name]?.next_cursor;
    if (!next) return false;
    state = {
      ...state,
      cursors: Object.freeze({ ...state.cursors, [name]: next }),
      histories: Object.freeze({
        ...state.histories,
        [name]: Object.freeze([...state.histories[name], state.cursors[name]]),
      }),
      data: Object.freeze({ ...state.data, [name]: null }),
    };
    state.phase = derivePhase(state);
    publish();
    return true;
  }

  function previousPage(tab) {
    const name = dataTab(tab);
    if (!state.histories[name].length) return false;
    const history = [...state.histories[name]];
    const cursor = history.pop() ?? null;
    state = {
      ...state,
      cursors: Object.freeze({ ...state.cursors, [name]: cursor }),
      histories: Object.freeze({ ...state.histories, [name]: Object.freeze(history) }),
      data: Object.freeze({ ...state.data, [name]: null }),
    };
    state.phase = derivePhase(state);
    publish();
    return true;
  }

  function setFeedback(feedback) {
    state = { ...state, feedback: String(feedback ?? "").slice(0, 160), error: null };
    publish();
  }

  function setIntent(kind, value) {
    const name = String(kind ?? "");
    if (!Object.hasOwn(state.intents, name)) throw new TypeError("invalid growth intent");
    state = {
      ...state,
      intents: Object.freeze({
        ...state.intents,
        [name]: value ? Object.freeze({ ...value }) : null,
      }),
    };
    publish();
  }

  function snapshot() {
    return Object.freeze({
      ...state,
      loading: Object.freeze([...state.loading]),
      availableTabs: Object.freeze([...state.availableTabs]),
      filters: freezeMap(state.filters),
      cursors: Object.freeze({ ...state.cursors }),
      histories: Object.freeze(Object.fromEntries(
        Object.entries(state.histories).map(([key, value]) => [key, Object.freeze([...value])]),
      )),
      data: Object.freeze({ ...state.data }),
      intents: Object.freeze({ ...state.intents }),
    });
  }

  function clearRequests() {
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
    selectTab,
    configure,
    nextPage,
    previousPage,
    setFeedback,
    setIntent,
    snapshot,
  });
}

export function platformGrowthOwnerSignature(context, navigationItems) {
  return ownerSignature(context, navigationItems);
}

export function platformGrowthTabs(permissions) {
  const values = new Set(permissions ?? []);
  if (values.has("platform.growth.read")) {
    return Object.freeze(["funnels", "cohorts", "campaigns", "experiments", "attribution"]);
  }
  const tabs = [];
  if (values.has("platform.growth.adoption_read")) tabs.push("funnels", "cohorts");
  if (values.has("platform.growth.channel_attribution_read")) tabs.push("attribution");
  return Object.freeze(tabs);
}

function ownerSignature(context, navigationItems) {
  const operator = context?.operator;
  const permissions = Array.isArray(context?.permissions) ? [...context.permissions].sort() : [];
  const navigation = Array.isArray(navigationItems) ? navigationItems : [];
  if (!navigation.some((item) => item?.id === "growth")
    || !/^pto_[0-9a-f]{32}$/u.test(String(operator?.operator_id ?? ""))
    || !Number.isSafeInteger(operator?.revision) || operator.revision < 1
    || !permissions.some((permission) => READ_PERMISSIONS.has(permission))) return "";
  return JSON.stringify({
    operator_id: operator.operator_id,
    operator_revision: operator.revision,
    roles: Array.isArray(operator.roles) ? [...operator.roles].sort() : [],
    permissions,
    expires_at: String(context.expires_at ?? ""),
    navigation: "growth",
  });
}

function initialState(permissions) {
  const availableTabs = platformGrowthTabs(permissions);
  return {
    phase: "idle",
    loading: Object.freeze([]),
    availableTabs,
    tab: availableTabs[0] ?? "funnels",
    filters: freezeMap(Object.fromEntries([...DATA_TABS].map((tab) => [tab, {}]))),
    cursors: Object.freeze(Object.fromEntries([...DATA_TABS].map((tab) => [tab, null]))),
    histories: Object.freeze(Object.fromEntries(
      [...DATA_TABS].map((tab) => [tab, Object.freeze([])]),
    )),
    data: Object.freeze(Object.fromEntries([...DATA_TABS].map((tab) => [tab, null]))),
    export: null,
    feedback: "",
    error: null,
    intents: Object.freeze({ campaign: null, experiment: null, export: null, approval: null }),
  };
}

function derivePhase(value) {
  if (value.error) return value.phase;
  const response = value.data[value.tab];
  if (response === null) return value.loading.includes(`read-${value.tab}`) ? "loading" : "idle";
  if (response.source_freshness?.status === "stale") return "stale";
  if (response.source_freshness?.status === "partial") return "partial";
  return response.items.length ? "ready" : "empty";
}

function hasFacts(value) {
  return Boolean(
    Object.values(value.data).some(Boolean)
    || value.export || value.feedback || value.error || value.loading.length,
  );
}

function safeError(error) {
  return Object.freeze({
    status: Number(error?.status) || 0,
    code: safeText(error?.code, "platform.request_failed"),
    message: safeText(error?.message, "Platform Growth 请求未完成。"),
    requestId: safeText(error?.requestId, "-"),
  });
}

function safeText(value, fallback) {
  if (typeof value !== "string" || !value) return fallback;
  return value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240);
}

function dataTab(value) {
  const tab = String(value ?? "");
  if (!DATA_TABS.has(tab)) throw new TypeError("invalid growth tab");
  return tab;
}

function laneName(value) {
  const lane = String(value ?? "");
  if (!/^[a-z][a-z-]{1,31}$/u.test(lane)) throw new TypeError("invalid request lane");
  return lane;
}

function freezeMap(value) {
  return Object.freeze(Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, Object.freeze({ ...item })]),
  ));
}
