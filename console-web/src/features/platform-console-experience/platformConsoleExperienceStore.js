const VIEWS = new Set(["overview", "admin-assistant", "approvals"]);

export function createPlatformConsoleExperienceStore() {
  let ownerKey = "";
  let view = "overview";
  let generation = 0;
  let operation = null;
  let status = "idle";
  let overview = null;
  let assistantDraft = "";
  let assistantResponse = null;
  let approvals = null;
  let approvalFilters = { status: "", risk: "", sourceDomain: "" };
  let approvalCursor = null;
  let approvalNextCursor = null;
  let approvalHistory = [];
  let navigationEpoch = 0;
  let permissionRevision = 0;
  let permissionFingerprint = "";
  let error = null;

  function enter(owner, nextView) {
    const nextOwner = ownerKeyFor(owner);
    if (!nextOwner || !VIEWS.has(nextView)) {
      leave();
      return false;
    }
    if (ownerKey && ownerKey !== nextOwner) reset(true);
    if (view !== nextView) {
      abort();
      clearVisibleFacts();
    }
    ownerKey = nextOwner;
    view = nextView;
    return true;
  }

  function begin(kind) {
    if (!ownerKey || operation) return null;
    generation += 1;
    status = "loading";
    error = null;
    if (kind === "assistant") assistantResponse = null;
    const controller = new AbortController();
    operation = Object.freeze({
      kind,
      generation,
      ownerKey,
      controller,
      signal: controller.signal,
      question: kind === "assistant" ? assistantDraft : undefined,
      filters: kind === "approvals" ? Object.freeze({ ...approvalFilters }) : undefined,
      cursor: kind === "approvals" ? approvalCursor : undefined,
    });
    return operation;
  }

  function accept(ticket, value, owner) {
    if (!current(ticket, owner)) return false;
    if (!responseCanAdvance(value)) {
      reject(ticket, {
        status: 409,
        code: "console_experience.response_regressed",
        message: "响应版本已回退，请刷新后重试",
      }, owner);
      return false;
    }
    if (ticket.kind === "overview") overview = value;
    else if (ticket.kind === "assistant") assistantResponse = value;
    else if (ticket.kind === "approvals") {
      approvals = value;
      approvalNextCursor = value.next_cursor;
    } else return false;
    navigationEpoch = value.navigation_epoch;
    permissionRevision = value.permission_revision;
    permissionFingerprint = value.permission_fingerprint;
    operation = null;
    status = value.status === "partial" || value.status === "stale" ? value.status : "ready";
    error = null;
    return true;
  }

  function reject(ticket, candidate, owner) {
    if (!current(ticket, owner)) return false;
    operation = null;
    if (candidate?.name === "AbortError") {
      status = "idle";
      return true;
    }
    const code = safeCode(candidate?.code);
    status = candidate?.status === 403
      ? "permission-revoked"
      : candidate?.status === 409
        ? "conflict"
        : candidate?.status === 503 || candidate?.status === 0
          ? "dependency-unavailable"
          : "error";
    error = Object.freeze({
      code,
      message: safeText(candidate?.message),
      requestId: safeChain(candidate?.requestId),
      traceId: safeChain(candidate?.traceId),
    });
    return true;
  }

  function setAssistantDraft(owner, value) {
    if (!ownerMatches(owner) || operation || typeof value !== "string" || [...value].length > 2000) return false;
    assistantDraft = value;
    if (!value.trim()) assistantResponse = null;
    status = value.trim() ? "draft" : "idle";
    error = null;
    return true;
  }

  function clearAssistant(owner) {
    if (!ownerMatches(owner)) return false;
    abort();
    assistantDraft = "";
    assistantResponse = null;
    error = null;
    status = "idle";
    return true;
  }

  function configureApprovals(owner, filters) {
    if (!ownerMatches(owner) || operation) return false;
    approvalFilters = {
      status: String(filters?.status ?? ""),
      risk: String(filters?.risk ?? ""),
      sourceDomain: String(filters?.sourceDomain ?? "").trim(),
    };
    approvalCursor = null;
    approvalNextCursor = null;
    approvalHistory = [];
    approvals = null;
    status = "idle";
    error = null;
    return true;
  }

  function nextApprovals(owner) {
    if (!ownerMatches(owner) || operation || !approvalNextCursor) return false;
    approvalHistory = [...approvalHistory, approvalCursor];
    approvalCursor = approvalNextCursor;
    approvalNextCursor = null;
    approvals = null;
    status = "idle";
    return true;
  }

  function previousApprovals(owner) {
    if (!ownerMatches(owner) || operation || approvalHistory.length === 0) return false;
    approvalCursor = approvalHistory.at(-1) ?? null;
    approvalHistory = approvalHistory.slice(0, -1);
    approvalNextCursor = null;
    approvals = null;
    status = "idle";
    return true;
  }

  function snapshot() {
    return Object.freeze({
      view,
      status,
      overview,
      assistant: Object.freeze({
        draft: assistantDraft,
        response: assistantResponse,
        canSubmit: Boolean(ownerKey && !operation && assistantDraft.trim()),
      }),
      approvals,
      approvalFilters: Object.freeze({ ...approvalFilters }),
      approvalPage: approvalHistory.length + 1,
      canPreviousApprovals: approvalHistory.length > 0 && !operation,
      canNextApprovals: Boolean(approvalNextCursor && !operation),
      busy: Boolean(operation),
      navigationEpoch,
      permissionRevision,
      permissionFingerprint,
      error,
    });
  }

  function leave() {
    reset(true);
  }

  function reset(clearOwner) {
    abort();
    clearVisibleFacts();
    assistantDraft = "";
    approvalFilters = { status: "", risk: "", sourceDomain: "" };
    approvalCursor = null;
    approvalNextCursor = null;
    approvalHistory = [];
    navigationEpoch = 0;
    permissionRevision = 0;
    permissionFingerprint = "";
    if (clearOwner) ownerKey = "";
  }

  function clearVisibleFacts() {
    overview = null;
    assistantResponse = null;
    approvals = null;
    error = null;
    status = "idle";
  }

  function abort() {
    generation += 1;
    const controller = operation?.controller;
    operation = null;
    controller?.abort();
  }

  function current(ticket, owner) {
    return Boolean(
      ticket
      && operation === ticket
      && !ticket.signal.aborted
      && ticket.generation === generation
      && ticket.ownerKey === ownerKey
      && ownerMatches(owner),
    );
  }

  function ownerMatches(owner) {
    return Boolean(ownerKey && ownerKey === ownerKeyFor(owner));
  }

  function responseCanAdvance(value) {
    const nextEpoch = Number(value?.navigation_epoch);
    const nextRevision = Number(value?.permission_revision);
    const nextFingerprint = String(value?.permission_fingerprint ?? "");
    return Number.isSafeInteger(nextEpoch) && nextEpoch >= Math.max(1, navigationEpoch)
      && Number.isSafeInteger(nextRevision) && nextRevision >= Math.max(1, permissionRevision)
      && !(nextRevision === permissionRevision && permissionFingerprint
        && nextFingerprint !== permissionFingerprint);
  }

  return Object.freeze({
    enter,
    begin,
    accept,
    reject,
    setAssistantDraft,
    clearAssistant,
    configureApprovals,
    nextApprovals,
    previousApprovals,
    snapshot,
    leave,
  });
}

function ownerKeyFor(owner) {
  if (!owner || typeof owner !== "object") return "";
  const operatorId = String(owner.operatorId ?? "");
  const sessionRevision = Number(owner.sessionRevision);
  const roles = Array.isArray(owner.roles) ? [...owner.roles].sort() : [];
  const permissions = Array.isArray(owner.permissions) ? [...owner.permissions].sort() : [];
  if (!/^[A-Za-z0-9_-]{3,160}$/u.test(operatorId)
    || !Number.isSafeInteger(sessionRevision) || sessionRevision < 1
    || roles.length === 0 || permissions.length === 0) return "";
  return JSON.stringify([operatorId, sessionRevision, roles, permissions]);
}

function safeCode(value) {
  const text = String(value ?? "");
  return /^(?:platform|console_experience)\.[a-z0-9_.]{1,120}$/u.test(text)
    ? text
    : "platform.console.request_failed";
}
function safeText(value) {
  const text = String(value ?? "");
  return text && text.length <= 240 && !/[\u0000-\u001f\u007f-\u009f]/u.test(text)
    ? text
    : "平台控制台请求未完成。";
}
function safeChain(value) {
  const text = String(value ?? "");
  return /^[A-Za-z0-9_.:-]{1,160}$/u.test(text) ? text : "-";
}
