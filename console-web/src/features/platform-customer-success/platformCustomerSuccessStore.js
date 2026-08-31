/** Owner-bound state for the PRD-140 Platform customer-success workbench. */

const READ_PERMISSIONS = new Set([
  "platform.customer_success.read",
  "platform.customer_success.support_read",
  "platform.customer_success.finance_read",
  "platform.customer_success.renewal.read",
]);
const TABS = new Set(["health", "plans", "tasks", "renewals", "qbr"]);

export function createPlatformCustomerSuccessStore({ onChange = () => {} } = {}) {
  let epoch = 0;
  let owner = "";
  const requests = new Map();
  let state = initialState();

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
      state = initialState();
      publish();
    }
    return true;
  }

  function leave() {
    const changed = owner || requests.size || hasFacts(state);
    clearRequests();
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
    state.loading = Object.freeze([...new Set([...state.loading, name])]);
    state.error = null;
    if (name === "directory" && state.directory === null) state.phase = "loading";
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
    const next = typeof update === "function" ? update(state) : update;
    state = {
      ...state,
      ...(next ?? {}),
      loading: Object.freeze(state.loading.filter((lane) => lane !== ticket.lane)),
      error: null,
    };
    state.phase = derivePhase(state);
    publish();
    return true;
  }

  function reject(ticket, error) {
    if (!isCurrent(ticket) || isAbort(error)) return false;
    requests.delete(ticket.lane);
    const status = Number(error?.status) || 0;
    if (status === 403) {
      clearRequests();
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

  function configureDirectory(filters = {}) {
    state = {
      ...state,
      filters: Object.freeze({ ...filters }),
      directoryCursor: null,
      directoryHistory: Object.freeze([]),
      directory: null,
      selectedAccountId: null,
      detail: null,
      health: null,
      renewals: null,
      contacts: null,
      contactsVisible: false,
    };
    publish();
  }

  function nextDirectoryPage() {
    const cursor = state.directory?.next_cursor;
    if (!cursor) return false;
    state = {
      ...state,
      directoryHistory: Object.freeze([...state.directoryHistory, state.directoryCursor]),
      directoryCursor: cursor,
      directory: null,
    };
    publish();
    return true;
  }

  function previousDirectoryPage() {
    if (!state.directoryHistory.length) return false;
    const history = [...state.directoryHistory];
    const cursor = history.pop() ?? null;
    state = {
      ...state,
      directoryHistory: Object.freeze(history),
      directoryCursor: cursor,
      directory: null,
    };
    publish();
    return true;
  }

  function selectAccount(accountId) {
    const id = String(accountId ?? "");
    if (!state.directory?.items.some((item) => item.customer_account_id === id)) return false;
    if (state.selectedAccountId === id) return true;
    abortDetailLanes();
    state = {
      ...state,
      selectedAccountId: id,
      detail: null,
      health: null,
      renewals: null,
      contacts: null,
      contactsVisible: false,
      tab: "health",
      error: null,
    };
    publish();
    return true;
  }

  function selectTab(tab) {
    const value = String(tab ?? "");
    if (!TABS.has(value) || !state.selectedAccountId) return false;
    state = { ...state, tab: value, error: null };
    publish();
    return true;
  }

  function hideContacts() {
    requests.get("contacts")?.controller.abort();
    requests.delete("contacts");
    state = {
      ...state,
      contacts: null,
      contactsVisible: false,
      loading: Object.freeze(state.loading.filter((lane) => lane !== "contacts")),
    };
    publish();
  }

  function setFeedback(feedback) {
    state = { ...state, feedback: String(feedback ?? "").slice(0, 160), error: null };
    publish();
  }

  function setIntent(kind, value) {
    state = {
      ...state,
      intents: Object.freeze({ ...state.intents, [kind]: value ? Object.freeze({ ...value }) : null }),
    };
    publish();
  }

  function snapshot() {
    return Object.freeze({
      ...state,
      filters: Object.freeze({ ...state.filters }),
      loading: Object.freeze([...state.loading]),
      directoryHistory: Object.freeze([...state.directoryHistory]),
      intents: Object.freeze({ ...state.intents }),
    });
  }

  function clearRequests() {
    for (const ticket of requests.values()) ticket.controller.abort();
    requests.clear();
  }

  function abortDetailLanes() {
    for (const lane of ["detail", "health", "renewals", "contacts"]) {
      requests.get(lane)?.controller.abort();
      requests.delete(lane);
    }
    state.loading = Object.freeze(
      state.loading.filter((lane) => !["detail", "health", "renewals", "contacts"].includes(lane)),
    );
  }

  return Object.freeze({
    enter,
    leave,
    begin,
    isCurrent,
    accept,
    reject,
    configureDirectory,
    nextDirectoryPage,
    previousDirectoryPage,
    selectAccount,
    selectTab,
    hideContacts,
    setFeedback,
    setIntent,
    snapshot,
  });
}

export function platformCustomerSuccessOwnerSignature(context, navigationItems) {
  return ownerSignature(context, navigationItems);
}

function ownerSignature(context, navigationItems) {
  const operator = context?.operator;
  const permissions = Array.isArray(context?.permissions) ? [...context.permissions].sort() : [];
  const navigation = Array.isArray(navigationItems) ? navigationItems : [];
  const visible = navigation.some((item) => item?.id === "customer-success");
  if (!visible || !/^pto_[0-9a-f]{32}$/u.test(String(operator?.operator_id ?? ""))
    || !Number.isSafeInteger(operator?.revision) || operator.revision < 1
    || !permissions.some((permission) => READ_PERMISSIONS.has(permission))) return "";
  const roles = Array.isArray(operator.roles) ? [...operator.roles].sort() : [];
  return JSON.stringify({
    operator_id: operator.operator_id,
    operator_revision: operator.revision,
    roles,
    permissions,
    expires_at: String(context.expires_at ?? ""),
    navigation: "customer-success",
  });
}

function initialState() {
  return {
    phase: "idle",
    loading: Object.freeze([]),
    filters: Object.freeze({}),
    directoryCursor: null,
    directoryHistory: Object.freeze([]),
    directory: null,
    selectedAccountId: null,
    tab: "health",
    detail: null,
    health: null,
    renewals: null,
    contacts: null,
    contactsVisible: false,
    batch: null,
    feedback: "",
    error: null,
    intents: Object.freeze({ plan: null, task: null, batch: null }),
  };
}

function derivePhase(value) {
  if (value.error) return value.phase;
  if (value.directory === null) return value.loading.includes("directory") ? "loading" : "idle";
  if (!value.directory.items.length) return "empty";
  if (value.health?.freshness === "stale") return "stale";
  if (value.detail && (value.detail.plans_truncated || value.detail.tasks_truncated
    || value.detail.qbr_truncated || value.detail.operations_truncated)) return "partial";
  return "ready";
}

function hasFacts(value) {
  return Boolean(
    value.directory || value.detail || value.health || value.renewals || value.contacts
    || value.batch || value.feedback || value.error || value.loading.length,
  );
}

function safeError(error) {
  return Object.freeze({
    status: Number(error?.status) || 0,
    code: safeText(error?.code, "platform.request_failed"),
    message: safeText(error?.message, "客户成功请求未完成。"),
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

function isAbort(error) {
  return error?.name === "AbortError";
}
