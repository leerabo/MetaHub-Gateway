/** Owner-scoped PRD-147 state. Cursors and retry keys never enter render snapshots. */

const READ_PERMISSION = "platform.console_migration.read";

export function createConsoleMigrationStore({ onChange = () => {} } = {}) {
  let ownerKey = "";
  let generation = 0;
  let operation = null;
  let phase = "idle";
  let response = null;
  let filters = { domainCode: "", state: "", workspaceRef: "" };
  let cursor = "";
  let nextCursor = "";
  let history = [];
  let intent = null;
  let feedback = null;
  let error = null;

  function enter(context, navigation) {
    const nextOwner = ownerKeyFor(context);
    const visible = Array.isArray(navigation)
      && navigation.some((item) => item?.id === "console-migration");
    if (!nextOwner || !visible || !context.permissions.includes(READ_PERMISSION)) {
      leave();
      return false;
    }
    if (ownerKey && ownerKey !== nextOwner) reset();
    ownerKey = nextOwner;
    emit();
    return true;
  }

  function begin(kind, details = null) {
    if (!ownerKey || operation) return null;
    generation += 1;
    const controller = new AbortController();
    operation = Object.freeze({
      kind,
      details,
      generation,
      ownerKey,
      controller,
      signal: controller.signal,
      filters: Object.freeze({ ...filters }),
      cursor,
    });
    phase = kind === "load" && response ? "refreshing" : "loading";
    error = null;
    feedback = null;
    emit();
    return operation;
  }

  function acceptLoad(ticket, value) {
    if (!current(ticket) || ticket.kind !== "load" || !canAdvance(value)) return false;
    nextCursor = value.next_cursor ?? "";
    response = Object.freeze({ ...value, next_cursor: null });
    operation = null;
    phase = value.contract.blocking_check_count > 0 ? "partial" : "ready";
    error = null;
    emit();
    return true;
  }

  function acceptCommand(ticket, value) {
    if (!current(ticket) || ticket.kind !== "command") return false;
    operation = null;
    phase = response ? "ready" : "idle";
    feedback = Object.freeze({
      code: value.idempotent_replay ? "idempotent_replay" : "accepted",
      operationId: value.operation_id,
      requestId: value.request_id,
    });
    intent = null;
    emit();
    return true;
  }

  function reject(ticket, candidate) {
    if (!current(ticket)) return false;
    operation = null;
    if (candidate?.name === "AbortError") {
      phase = response ? "ready" : "idle";
      emit();
      return true;
    }
    const status = Number(candidate?.status) || 0;
    phase = status === 401 ? "session-invalid"
      : status === 403 ? "permission-revoked"
        : status === 409 ? "conflict"
          : status === 503 || status === 0 ? "dependency-unavailable" : "error";
    error = Object.freeze({
      code: safeCode(candidate?.code),
      requestId: safeChain(candidate?.requestId),
      traceId: safeChain(candidate?.traceId),
    });
    emit();
    return true;
  }

  function configure(nextFilters) {
    if (!ownerKey || operation) return false;
    filters = {
      domainCode: String(nextFilters?.domainCode ?? ""),
      state: String(nextFilters?.state ?? ""),
      workspaceRef: String(nextFilters?.workspaceRef ?? ""),
    };
    cursor = "";
    nextCursor = "";
    history = [];
    response = null;
    phase = "idle";
    feedback = null;
    error = null;
    emit();
    return true;
  }

  function move(direction) {
    if (!ownerKey || operation) return false;
    if (direction === "next" && nextCursor) {
      history = [...history, cursor];
      cursor = nextCursor;
    } else if (direction === "previous" && history.length) {
      cursor = history.at(-1) ?? "";
      history = history.slice(0, -1);
    } else return false;
    nextCursor = "";
    response = null;
    phase = "idle";
    error = null;
    emit();
    return true;
  }

  function commandIntent(fingerprint, createKey) {
    if (!ownerKey || operation || typeof fingerprint !== "string"
      || typeof createKey !== "function") return null;
    if (!intent || intent.fingerprint !== fingerprint) {
      intent = Object.freeze({ fingerprint, idempotencyKey: createKey() });
    }
    return intent;
  }

  function clearIntent() {
    intent = null;
  }

  function isCurrent(ticket) {
    return current(ticket);
  }

  function leave() {
    reset();
    ownerKey = "";
    emit();
  }

  function reset() {
    operation?.controller.abort();
    generation += 1;
    operation = null;
    phase = "idle";
    response = null;
    filters = { domainCode: "", state: "", workspaceRef: "" };
    cursor = "";
    nextCursor = "";
    history = [];
    intent = null;
    feedback = null;
    error = null;
  }

  function snapshot() {
    return Object.freeze({
      phase,
      busy: operation !== null,
      response,
      filters: Object.freeze({ ...filters }),
      canPrevious: history.length > 0,
      canNext: Boolean(nextCursor),
      feedback,
      error,
    });
  }

  function current(ticket) {
    return Boolean(ticket && operation === ticket && ownerKey
      && ticket.ownerKey === ownerKey && ticket.generation === generation
      && !ticket.signal.aborted);
  }

  function canAdvance(value) {
    if (!value || !response) return Boolean(value);
    if (value.contract.contract_generation < response.contract.contract_generation
      || value.scheduler.registry_generation < response.scheduler.registry_generation) {
      phase = "conflict";
      error = Object.freeze({
        code: "console_migration.response_regressed",
        requestId: value.request_id,
        traceId: value.trace_id,
      });
      operation = null;
      emit();
      return false;
    }
    return true;
  }

  function emit() { onChange(snapshot()); }

  return Object.freeze({
    enter,
    begin,
    acceptLoad,
    acceptCommand,
    reject,
    configure,
    move,
    commandIntent,
    clearIntent,
    isCurrent,
    leave,
    snapshot,
  });
}

function ownerKeyFor(context) {
  const operator = context?.operator;
  const operatorId = String(operator?.operator_id ?? "");
  const revision = Number(operator?.revision);
  const roles = Array.isArray(operator?.roles) ? [...operator.roles].map(String).sort() : [];
  const permissions = Array.isArray(context?.permissions)
    ? [...context.permissions].map(String).sort()
    : [];
  if (!/^pto_[0-9a-f]{32}$/u.test(operatorId) || operator?.status !== "active"
    || !Number.isSafeInteger(revision) || revision < 1 || roles.length === 0
    || permissions.length === 0) return "";
  return JSON.stringify([operatorId, revision, roles, permissions, String(context?.expires_at ?? "")]);
}

function safeCode(value) {
  const text = String(value ?? "");
  return /^console_migration\.[a-z0-9_.-]{2,100}$/u.test(text)
    ? text
    : "console_migration.dependency_unavailable";
}
function safeChain(value) {
  const text = String(value ?? "");
  return /^[A-Za-z0-9_.:-]{1,160}$/u.test(text) ? text : "-";
}
