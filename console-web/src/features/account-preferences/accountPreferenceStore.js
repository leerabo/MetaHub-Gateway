export function createAccountPreferenceStore({ createIdempotencyKey } = {}) {
  if (typeof createIdempotencyKey !== "function") throw new TypeError("account preference store requires idempotency factory");
  let ownerKey = "";
  let generation = 0;
  let operation = null;
  let preference = null;
  let status = "idle";
  let error = null;
  let intent = null;

  function enter(owner) {
    const nextOwnerKey = ownerKeyFor(owner);
    if (!nextOwnerKey) {
      leave();
      return false;
    }
    if (ownerKey && ownerKey !== nextOwnerKey) reset(true);
    ownerKey = nextOwnerKey;
    return true;
  }

  function beginLoad(owner) {
    if (!ownerMatches(owner) || operation) return null;
    return begin("load");
  }

  function beginSave(owner, locale) {
    if (!ownerMatches(owner) || operation || !preference
      || !preference.available_locales.includes(locale)) return null;
    const fingerprint = JSON.stringify([locale, preference.revision]);
    if (!intent || intent.fingerprint !== fingerprint) {
      intent = Object.freeze({
        fingerprint,
        locale,
        revision: preference.revision,
        idempotencyKey: createIdempotencyKey(),
      });
    }
    const ticket = begin("save");
    return Object.freeze({ ...ticket, ...intent });
  }

  function begin(kind) {
    generation += 1;
    status = "loading";
    error = null;
    const controller = new AbortController();
    operation = Object.freeze({
      kind,
      generation,
      ownerKey,
      controller,
      signal: controller.signal,
    });
    return operation;
  }

  function accept(ticket, value, owner) {
    if (!current(ticket, owner)) return false;
    if (preference && Number(value?.revision) < preference.revision) {
      reject(ticket, {
        status: 409,
        code: "account_preference.revision_regressed",
        message: "偏好版本已回退，请刷新后重试",
      }, owner);
      return false;
    }
    operation = null;
    preference = value;
    intent = null;
    status = "ready";
    error = null;
    return true;
  }

  function reject(ticket, candidate, owner) {
    if (!current(ticket, owner)) return false;
    operation = null;
    status = candidate?.status === 409
      ? "conflict"
      : candidate?.status === 401 || candidate?.status === 403
        ? "permission-revoked"
        : candidate?.status === 503 || candidate?.status === 0
          ? "dependency-unavailable"
          : "error";
    if (candidate?.status > 0 && candidate.status < 500) intent = null;
    error = Object.freeze({
      code: safeCode(candidate?.code),
      message: safeText(candidate?.message),
      requestId: safeChain(candidate?.requestId),
      traceId: safeChain(candidate?.traceId),
    });
    return true;
  }

  function snapshot() {
    return Object.freeze({
      preference,
      status,
      busy: Boolean(operation),
      error,
    });
  }

  function leave() {
    reset(true);
  }

  function reset(clearOwner) {
    generation += 1;
    const controller = operation?.controller;
    operation = null;
    controller?.abort();
    preference = null;
    status = "idle";
    error = null;
    intent = null;
    if (clearOwner) ownerKey = "";
  }

  function current(ticket, owner) {
    return Boolean(
      ticket
      && operation
      && ticket.kind === operation.kind
      && ticket.generation === operation.generation
      && ticket.ownerKey === operation.ownerKey
      && !operation.signal.aborted
      && ownerMatches(owner),
    );
  }

  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKey === ownerKeyFor(owner));
  }

  return Object.freeze({
    enter,
    beginLoad,
    beginSave,
    accept,
    reject,
    snapshot,
    leave,
  });
}

function ownerKeyFor(owner) {
  const accountId = String(owner?.accountId ?? "");
  const sessionId = String(owner?.sessionId ?? "");
  return accountId && sessionId ? JSON.stringify([accountId, sessionId]) : "";
}
function safeCode(value) {
  const text = String(value ?? "");
  return /^(?:identity|account_preference)\.[a-z0-9_.]{1,120}$/u.test(text)
    ? text
    : "identity.preference.request_failed";
}
function safeText(value) {
  const text = String(value ?? "");
  return text && text.length <= 240 && !/[\u0000-\u001f\u007f-\u009f]/u.test(text)
    ? text
    : "账号偏好请求未完成。";
}
function safeChain(value) {
  const text = String(value ?? "");
  return /^[A-Za-z0-9_.:-]{1,160}$/u.test(text) ? text : "-";
}
