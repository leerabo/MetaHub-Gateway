const VIEWS = new Set([
  "overview", "admin-assistant", "approvals", "system-status", "deployment-summary",
]);

export function createWorkspaceConsoleExperienceStore() {
  let ownerKey = "";
  let expiresAt = 0;
  let view = "overview";
  let generation = 0;
  let operation = null;
  let status = "idle";
  let overview = null;
  let overviewPeriod = "month";
  let assistantDraft = "";
  let assistantResponse = null;
  let approvals = null;
  let deploymentSummary = null;
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
    if (ownerKey !== nextOwner || view !== nextView) reset(true);
    ownerKey = nextOwner;
    expiresAt = Math.min(Date.parse(owner.globalExpiresAt), Date.parse(owner.workspaceExpiresAt));
    permissionRevision = owner.permissionRevision;
    permissionFingerprint = owner.permissionFingerprint;
    view = nextView;
    return true;
  }

  function begin(kind) {
    if (Date.now() >= expiresAt) { reset(true); return null; }
    if (!ownerKey || operation) return null;
    if (kind !== (view === "admin-assistant" ? "assistant" : view)
      || (kind === "assistant" && !assistantDraft.trim())) return null;
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
    if (ticket.kind === "overview" || ticket.kind === "system-status") overview = value;
    else if (ticket.kind === "assistant") assistantResponse = value;
    else if (ticket.kind === "approvals") {
      approvals = value;
      approvalNextCursor = value.next_cursor;
    } else if (ticket.kind === "deployment-summary") deploymentSummary = value;
    else return false;
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
    if ([401, 403, 409].includes(candidate?.status)) reset(true);
    status = candidate?.status === 403
      ? "permission-revoked"
      : candidate?.status === 409
        ? "conflict"
        : candidate?.status === 503 || candidate?.status === 0
          ? "dependency-unavailable"
          : "error";
    error = Object.freeze({
      code: safeCode(candidate?.code),
      message: safeText(candidate?.message),
      requestId: safeChain(candidate?.requestId),
      traceId: safeChain(candidate?.traceId),
    });
    return true;
  }

  function setOverviewPeriod(owner, value) {
    if (view !== "overview" || !ownerMatches(owner) || Date.now() >= expiresAt
      || !["day", "month", "all"].includes(value)) return false;
    overviewPeriod = value;
    return true;
  }

  function setAssistantDraft(owner, value) {
    if (view !== "admin-assistant" || !ownerMatches(owner) || operation
      || typeof value !== "string" || [...value].length > 2000) return false;
    assistantDraft = value;
    if (!value.trim()) assistantResponse = null;
    status = value.trim() ? "draft" : "idle";
    error = null;
    return true;
  }

  function clearAssistant(owner) {
    if (view !== "admin-assistant" || !ownerMatches(owner)) return false;
    abort();
    assistantDraft = "";
    assistantResponse = null;
    error = null;
    status = "idle";
    return true;
  }

  function configureApprovals(owner, filters) {
    if (view !== "approvals" || !ownerMatches(owner) || operation) return false;
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
    if (view !== "approvals" || !ownerMatches(owner) || operation || !approvalNextCursor) return false;
    approvalHistory = [...approvalHistory, approvalCursor];
    approvalCursor = approvalNextCursor;
    approvalNextCursor = null;
    approvals = null;
    status = "idle";
    return true;
  }

  function previousApprovals(owner) {
    if (view !== "approvals" || !ownerMatches(owner) || operation || approvalHistory.length === 0) return false;
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
      overviewPeriod,
      assistant: Object.freeze({
        draft: assistantDraft,
        response: assistantResponse,
        canSubmit: Boolean(ownerKey && !operation && assistantDraft.trim()),
      }),
      approvals,
      deploymentSummary,
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
    overviewPeriod = "month";
    approvalFilters = { status: "", risk: "", sourceDomain: "" };
    approvalCursor = null;
    approvalNextCursor = null;
    approvalHistory = [];
    navigationEpoch = 0;
    permissionRevision = 0;
    permissionFingerprint = "";
    if (clearOwner) { ownerKey = ""; expiresAt = 0; }
  }

  function clearVisibleFacts() {
    overview = null;
    assistantResponse = null;
    approvals = null;
    deploymentSummary = null;
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
    if (Date.now() >= expiresAt) { reset(true); return false; }
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
    const nextEpoch = value?.navigation_epoch;
    const nextRevision = value?.permission_revision;
    const nextFingerprint = value?.permission_fingerprint;
    return Number.isSafeInteger(nextEpoch) && nextEpoch >= Math.max(1, navigationEpoch)
      && nextRevision === permissionRevision
      && nextFingerprint === permissionFingerprint;
  }

  return Object.freeze({
    enter,
    begin,
    accept,
    reject,
    setAssistantDraft,
    setOverviewPeriod,
    clearAssistant,
    configureApprovals,
    nextApprovals,
    previousApprovals,
    snapshot,
    leave,
    owns: (owner, candidateView) => view === candidateView && ownerMatches(owner),
  });
}

function ownerKeyFor(owner) {
  if (!owner || typeof owner !== "object") return "";
  const permissions = owner.permissions;
  if (!/^wsp_[0-9a-f]{32}$/u.test(owner.workspaceId)
    || !/^(?:owner|admin|developer|security_admin|finance_admin|legal_admin|auditor|viewer)$/u.test(owner.role)
    || typeof owner.globalSessionId !== "string" || !/^[a-zA-Z0-9_-]{1,160}$/u.test(owner.globalSessionId)
    || !Number.isSafeInteger(owner.revokeGeneration) || owner.revokeGeneration < 0
    || !Number.isSafeInteger(owner.permissionRevision) || owner.permissionRevision < 1
    || !Number.isSafeInteger(owner.membershipRevision) || owner.membershipRevision < 1
    || !/^[0-9a-f]{64}$/u.test(owner.permissionFingerprint)
    || !/^ncs_[0-9a-f]{32}$/u.test(owner.contractSnapshotId)
    || !Number.isSafeInteger(owner.contractGeneration) || owner.contractGeneration < 1
    || !Number.isSafeInteger(owner.navigationEpoch) || owner.navigationEpoch < 1
    || ![owner.globalExpiresAt, owner.workspaceExpiresAt].every((value) =>
      typeof value === "string" && Number.isFinite(Date.parse(value)) && Date.parse(value) > Date.now())
    || !Array.isArray(permissions) || permissions.length === 0
    || !permissions.every((permission) => typeof permission === "string")) return "";
  return JSON.stringify([
    owner.workspaceId, owner.role, owner.globalSessionId, owner.revokeGeneration,
    owner.globalExpiresAt, owner.workspaceExpiresAt, owner.permissionRevision,
    owner.membershipRevision, owner.permissionFingerprint,
    owner.contractSnapshotId, owner.contractGeneration, owner.navigationEpoch, [...permissions].sort(),
  ]);
}

function safeCode(value) {
  const text = String(value ?? "");
  return /^(?:workspace_session|workspace|console_experience)\.[a-z0-9_.]{1,120}$/u.test(text)
    ? text
    : "workspace.console.request_failed";
}
function safeText(value) {
  const text = String(value ?? "");
  return text && text.length <= 240 && !/[\u0000-\u001f\u007f-\u009f]/u.test(text)
    ? text
    : "Workspace 控制台请求未完成。";
}
function safeChain(value) {
  const text = String(value ?? "");
  return /^[A-Za-z0-9_.:-]{1,160}$/u.test(text) ? text : "-";
}
