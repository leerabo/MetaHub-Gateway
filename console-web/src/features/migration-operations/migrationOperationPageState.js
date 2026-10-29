/** PRD-131 owner-bound in-memory workflow state for Workspace migration operations. */

import {
  authorizationFingerprint,
  authorizationIdentitySnapshot,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "../../lib/permissions.js";

const PAGE_ID = "migration-operations";
const LANES = Object.freeze(["assessment", "differences", "run", "transition"]);
const SAFE_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u;
const SAFE_CHAIN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;

export function migrationOperationReadIdentitySnapshot(authContext) {
  return authorizationIdentitySnapshot(authContext, PERMISSIONS.MIGRATION_READ);
}

export function migrationOperationPageOwner(authContext, consoleShellState) {
  const readIdentity = migrationOperationReadIdentitySnapshot(authContext);
  const manageIdentity = hasPermission(authContext, PERMISSIONS.MIGRATION_MANAGE)
    ? authorizationIdentitySnapshot(authContext, PERMISSIONS.MIGRATION_MANAGE)
    : "";
  const permissions = permissionCodesFromAuthContext(authContext);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const navigation = consoleShellState?.navigation;
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === PAGE_ID)
    : null;
  const required = normalizePermissions(item?.required_permissions);
  const navigationPermissions = normalizePermissions(navigation?.permission_codes);
  if (
    !readIdentity
    || consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== PAGE_ID
    || navigation?.active_navigation_item_id !== PAGE_ID
    || item?.target_section !== PAGE_ID
    || item?.visible !== true
    || item?.disabled !== false
    || JSON.stringify(required) !== JSON.stringify([PERMISSIONS.MIGRATION_READ])
    || JSON.stringify(navigationPermissions) !== JSON.stringify(permissions)
    || Number(navigation?.permission_revision) !== revision
    || String(navigation?.authorization_fingerprint ?? "") !== fingerprint
  ) return null;
  return Object.freeze({
    page: PAGE_ID,
    tenantId: String(authContext?.user?.tenant_id ?? authContext?.tenant?.tenant_id ?? ""),
    accountId: String(authContext?.user?.account_id ?? authContext?.user?.user_id ?? ""),
    readIdentity,
    manageIdentity,
    navigationOwner: JSON.stringify([
      item.id, item.target_section, required, permissions, revision, fingerprint,
    ]),
    canManage: Boolean(manageIdentity),
  });
}

export function createMigrationOperationPageState({ clearDom = () => {} } = {}) {
  let ownerKey = "";
  let pageGeneration = 0;
  let state = emptyState();
  const intentKeys = new Map();
  const lanes = new Map(LANES.map((name) => [name, {
    generation: 0,
    controller: null,
    ticket: null,
  }]));

  const identityKey = (owner) => owner ? JSON.stringify([
    owner.readIdentity, owner.manageIdentity, owner.navigationOwner,
  ]) : "";

  const abortLane = (name) => {
    const lane = lanes.get(name);
    lane?.controller?.abort();
    if (lane) {
      lane.controller = null;
      lane.ticket = null;
    }
  };

  const abortAll = () => {
    for (const name of LANES) abortLane(name);
  };

  const clearInternal = (nextOwnerKey = "") => {
    clearDom();
    abortAll();
    intentKeys.clear();
    state = emptyState();
    ownerKey = nextOwnerKey;
    pageGeneration += 1;
  };

  const isCurrent = (ticket, owner) => {
    const lane = lanes.get(ticket?.lane);
    return Boolean(
      ticket
      && lane?.ticket === ticket
      && ticket.ownerKey === ownerKey
      && ticket.pageGeneration === pageGeneration
      && ticket.generation === lane.generation
      && identityKey(owner) === ownerKey
      && !ticket.signal.aborted
    );
  };

  const beginLane = (name, owner, details = {}) => {
    const lane = lanes.get(name);
    if (!lane || !ownerKey || identityKey(owner) !== ownerKey) return null;
    abortLane(name);
    lane.generation += 1;
    lane.controller = new AbortController();
    lane.ticket = Object.freeze({
      lane: name,
      generation: lane.generation,
      pageGeneration,
      ownerKey,
      controller: lane.controller,
      signal: lane.controller.signal,
      ...details,
    });
    state = freezeState({
      ...state,
      busy: { ...state.busy, [name]: true },
      feedback: null,
    });
    return lane.ticket;
  };

  return {
    enter(owner) {
      const next = identityKey(owner);
      if (next !== ownerKey) clearInternal(next);
      return Boolean(ownerKey);
    },

    snapshot() {
      return state;
    },

    idempotencyKey(owner, scope, intent, createKey) {
      if (identityKey(owner) !== ownerKey || !["assessment", "run", "transition"].includes(scope)
        || typeof createKey !== "function") return "";
      const signature = JSON.stringify(intent);
      const current = intentKeys.get(scope);
      if (current?.signature === signature) return current.key;
      const key = createKey();
      intentKeys.set(scope, Object.freeze({ signature, key }));
      return key;
    },

    completeIntent(owner, scope) {
      if (identityKey(owner) !== ownerKey) return false;
      return intentKeys.delete(scope);
    },

    beginAssessment(owner) {
      const ticket = beginLane("assessment", owner);
      if (!ticket) return null;
      abortLane("differences");
      abortLane("run");
      abortLane("transition");
      state = freezeState({
        ...emptyState(),
        busy: { ...emptyState().busy, assessment: true },
      });
      return ticket;
    },

    acceptAssessment(ticket, response, owner) {
      if (!isCurrent(ticket, owner) || !response?.assessment) return false;
      state = freezeState({
        ...state,
        assessment: response.assessment,
        run: null,
        differences: [],
        differencesFor: null,
        feedback: successFeedback(
          response.idempotent_replay ? "migration.assessment_replayed" : "migration.assessment_created",
          response.request_id,
        ),
      });
      return true;
    },

    beginDifferences(owner, migrationId) {
      const expected = state.run?.run_id ?? state.assessment?.assessment_id;
      if (!expected || migrationId !== expected) return null;
      return beginLane("differences", owner, { migrationId });
    },

    acceptDifferences(ticket, response, owner) {
      if (!isCurrent(ticket, owner) || !response || (
        response.run_id ?? response.assessment_id
      ) !== ticket.migrationId) return false;
      state = freezeState({
        ...state,
        differences: response.items,
        differencesFor: ticket.migrationId,
        feedback: response.support_scoped
          ? successFeedback("migration.differences_support_scoped", response.request_id)
          : state.feedback,
      });
      return true;
    },

    beginRun(owner) {
      const assessment = state.assessment;
      if (!owner?.canManage || !assessment || assessment.status !== "ready"
        || assessment.blocking_difference_count !== 0) return null;
      return beginLane("run", owner, {
        assessmentId: assessment.assessment_id,
        assessmentRevision: assessment.revision,
      });
    },

    acceptRun(ticket, response, owner) {
      if (!isCurrent(ticket, owner) || !response?.run
        || response.run.assessment_id !== ticket.assessmentId) return false;
      state = freezeState({
        ...state,
        run: response.run,
        differences: [],
        differencesFor: null,
        feedback: successFeedback(
          response.idempotent_replay ? "migration.run_replayed" : "migration.run_created",
          response.request_id,
        ),
      });
      return true;
    },

    openTransition(owner, action) {
      const run = state.run;
      if (!owner?.canManage || !run || !transitionAllowed(run, action)) return false;
      state = freezeState({
        ...state,
        dialog: Object.freeze({ action, busy: false, error: null }),
        feedback: null,
      });
      return true;
    },

    closeTransition(owner) {
      if (identityKey(owner) !== ownerKey || state.busy.transition) return false;
      state = freezeState({ ...state, dialog: null });
      return true;
    },

    beginTransition(owner, { action, reasonCode, idempotencyKey } = {}) {
      const run = state.run;
      if (!state.dialog || state.dialog.action !== action || !transitionAllowed(run, action)
        || !transitionReasonAllowed(action, reasonCode)) {
        return null;
      }
      const ticket = beginLane("transition", owner, {
        action,
        reasonCode,
        idempotencyKey,
        runId: run.run_id,
        revision: run.revision,
        workspaceId: state.assessment?.workspace_id ?? "",
      });
      if (!ticket) return null;
      state = freezeState({
        ...state,
        dialog: Object.freeze({ action, busy: true, error: null }),
      });
      return ticket;
    },

    acceptTransition(ticket, response, owner) {
      if (!isCurrent(ticket, owner) || !response?.run || response.run.run_id !== ticket.runId) {
        return false;
      }
      state = freezeState({
        ...state,
        run: response.run,
        differences: [],
        differencesFor: null,
        dialog: null,
        feedback: successFeedback(
          ticket.action === "cutover" ? "migration.cutover_completed" : "migration.rolled_back",
          response.request_id,
        ),
      });
      return true;
    },

    reject(ticket, error, owner) {
      if (!isCurrent(ticket, owner)) return false;
      const feedback = errorFeedback(error);
      state = freezeState({
        ...state,
        feedback,
        dialog: ticket.lane === "transition" && state.dialog
          ? Object.freeze({ ...state.dialog, busy: false, error: feedback })
          : state.dialog,
      });
      return true;
    },

    finish(ticket) {
      const lane = lanes.get(ticket?.lane);
      if (lane?.ticket !== ticket) return false;
      lane.ticket = null;
      lane.controller = null;
      state = freezeState({
        ...state,
        busy: { ...state.busy, [ticket.lane]: false },
        dialog: ticket.lane === "transition" && state.dialog
          ? Object.freeze({ ...state.dialog, busy: false })
          : state.dialog,
      });
      return true;
    },

    isCurrent,

    blocksShellDomReplacement() {
      return Boolean(state.dialog || Object.values(state.busy).some(Boolean));
    },

    clear() {
      clearInternal("");
    },
  };
}

export function isMigrationOperationAbortError(error) {
  return error?.name === "AbortError" || error?.code === "ABORT_ERR";
}

function transitionAllowed(run, action) {
  if (!run || run.rollback_plan?.status !== "ready") return false;
  if (action === "cutover") {
    return run.status === "observing" && run.blocking_difference_count === 0;
  }
  return action === "rollback" && ["observing", "completed", "failed"].includes(run.status);
}

function transitionReasonAllowed(action, reasonCode) {
  const choices = action === "cutover"
    ? ["customer_approved", "scheduled_window", "validation_recovered"]
    : action === "rollback"
      ? ["customer_requested_rollback", "validation_failed", "observation_failed"]
      : [];
  return choices.includes(reasonCode);
}

function emptyState() {
  return freezeState({
    assessment: null,
    differences: [],
    differencesFor: null,
    run: null,
    feedback: null,
    dialog: null,
    busy: Object.fromEntries(LANES.map((name) => [name, false])),
  });
}

function freezeState(value) {
  return Object.freeze({
    ...value,
    differences: Object.freeze([...(value.differences ?? [])]),
    busy: Object.freeze({ ...(value.busy ?? {}) }),
  });
}

function normalizePermissions(value) {
  if (!Array.isArray(value)) return null;
  const normalized = value.map((item) => String(item)).sort();
  if (new Set(normalized).size !== normalized.length) return null;
  return normalized;
}

function successFeedback(code, requestId) {
  return Object.freeze({ kind: "success", code, requestId: safeChain(requestId) });
}

function errorFeedback(error) {
  return Object.freeze({
    kind: "error",
    code: SAFE_CODE.test(String(error?.code ?? "")) ? String(error.code) : "migration.unavailable",
    requestId: safeChain(error?.requestId),
    status: Number.isSafeInteger(Number(error?.status)) ? Number(error.status) : 0,
  });
}

function safeChain(value) {
  const text = String(value ?? "");
  return SAFE_CHAIN.test(text) ? text : "-";
}
