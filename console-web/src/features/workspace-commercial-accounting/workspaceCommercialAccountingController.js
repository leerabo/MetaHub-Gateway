/** WorkspaceSession controller for PRD-144 subscription and Internal FinOps. */

import { AuthApiError } from "../../lib/auth.js";
import { createWorkspaceCommercialAccountingStore } from "./workspaceCommercialAccountingStore.js";

export function createWorkspaceCommercialAccountingController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
  newIdempotencyKey = defaultIdempotencyKey,
} = {}) {
  if (!api) throw new TypeError("workspace commercial-accounting controller requires api");
  const store = createWorkspaceCommercialAccountingStore({ onChange });
  let activation = 0;
  let activationController = null;
  let activationContext = null;
  let workspaceSession = null;

  async function open({ authContext, workspaceIdentity, activeNavigationItemId }) {
    const currentActivation = ++activation;
    activationController?.abort();
    activationController = new AbortController();
    activationContext = { authContext, workspaceIdentity, activeNavigationItemId };
    workspaceSession = null;
    store.prepare(activeNavigationItemId);
    try {
      const session = await api.workspaceSession({ signal: activationController.signal });
      if (currentActivation !== activation || activationController.signal.aborted) return false;
      if (!store.enter({ ...activationContext, workspaceSession: session })) {
        throw new AuthApiError({ status: 403, code: "workspace.authorization_mismatch",
          message: "组织授权已变化，请刷新页面。" });
      }
      workspaceSession = session;
      return isSubscriptionNavigation(activeNavigationItemId)
        ? loadSubscription()
        : loadRates();
    } catch (error) {
      if (error?.name === "AbortError" || currentActivation !== activation) return false;
      store.activationFailed(activeNavigationItemId, error);
      if (Number(error?.status) === 401) onSessionInvalid(error);
      if (Number(error?.status) === 403) onPermissionRevoked(error);
      return false;
    } finally {
      if (currentActivation === activation) activationController = null;
    }
  }

  function close() {
    activation += 1;
    activationController?.abort();
    activationController = null;
    activationContext = null;
    workspaceSession = null;
    store.leave();
  }

  async function refresh() {
    if (!workspaceSession || !activationContext) return false;
    return isSubscriptionNavigation(activationContext.activeNavigationItemId)
      ? loadSubscription()
      : loadRates();
  }

  async function loadSubscription() {
    if (!workspaceSession || !isSubscriptionNavigation(activationContext?.activeNavigationItemId)) {
      return false;
    }
    const ticket = store.begin("subscription");
    try {
      const subscription = await api.workspaceSubscription({
        period: "current",
        signal: ticket.controller.signal,
      });
      return store.accept(ticket, {
        subscription,
        permissionRevision: subscription.permission_revision,
        permissionFingerprint: subscription.permission_fingerprint,
      });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadRates() {
    if (!workspaceSession || !isRateNavigation(activationContext?.activeNavigationItemId)) {
      return false;
    }
    const snapshot = store.snapshot();
    const ticket = store.begin("rates");
    try {
      const [rates, fxRates, models] = await Promise.all([
        api.providerCostRates({
          filters: { ...compactObject(snapshot.rateFilters), include_history: true },
          cursor: snapshot.rateCursor, limit: 50, signal: ticket.controller.signal,
        }),
        typeof api.fxRates === "function"
          ? api.fxRates({ includeHistory: true, limit: 50, signal: ticket.controller.signal })
          : Promise.resolve(null),
        typeof api.models === "function"
          ? api.models({ signal: ticket.controller.signal })
          : Promise.resolve(null),
      ]);
      return store.accept(ticket, {
        rates,
        fxRates,
        models,
        permissionRevision: rates.permission_revision,
        permissionFingerprint: rates.permission_fingerprint,
      });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function applyFxRate({ command, expectedHeadRevision, resourceId }, stepUp) {
    const snapshot = store.snapshot();
    if (!workspaceSession || !isRateNavigation(snapshot.navigation) || !snapshot.canManageRates) return false;
    const fingerprint = JSON.stringify({ command, expectedHeadRevision, resourceId });
    return mutate({
      kind: "fx-rate", fingerprint, actionCode: "routing_fx_rate.create_version",
      resourceType: "workspace_fx_rate", resourceId: String(resourceId ?? "USD:CNY"), stepUp,
      invoke: (intent, grant, signal) => api.commandFxRate({
        command, expectedHeadRevision, idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant, signal,
      }),
      feedback: (response) => response.idempotent_replay ? "汇率设置已恢复。" : "USD/CNY 汇率已更新。",
      reload: loadRates,
    });
  }

  async function configureRates(filters = {}) {
    if (!workspaceSession || !isRateNavigation(activationContext?.activeNavigationItemId)) {
      return false;
    }
    store.configureRates(filters);
    return loadRates();
  }

  async function moveRatePage(direction) {
    const moved = direction === "next" ? store.nextRatePage() : store.previousRatePage();
    if (!moved) return false;
    return loadRates();
  }

  async function submitDispute({ command, expectedSourceRevision }, stepUp) {
    const snapshot = store.snapshot();
    if (!workspaceSession || !isSubscriptionNavigation(snapshot.navigation)
      || !snapshot.canSubmitDispute) return false;
    const fingerprint = JSON.stringify({ command, expectedSourceRevision });
    return mutate({
      kind: "dispute",
      fingerprint,
      actionCode: "commercial_dispute.submit",
      resourceType: "commercial_billing_source",
      resourceId: String(command?.source_id ?? ""),
      stepUp,
      invoke: (intent, grant, signal) => api.createDispute({
        command,
        expectedSourceRevision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      }),
      feedback: (response) => response.idempotent_replay
        ? "原争议提交结果已安全恢复。"
        : "账单争议已提交。",
      reload: loadSubscription,
    });
  }

  async function applyRate({ command, expectedHeadRevision, resourceId }, stepUp) {
    const snapshot = store.snapshot();
    if (!workspaceSession || !isRateNavigation(snapshot.navigation)
      || !snapshot.canManageRates) return false;
    const target = String(resourceId ?? rateResourceId(command));
    const fingerprint = JSON.stringify({ command, expectedHeadRevision, resourceId: target });
    return mutate({
      kind: "rate",
      fingerprint,
      actionCode: "provider_cost_rate." + String(command?.action ?? ""),
      resourceType: "workspace_provider_cost_rate",
      resourceId: target,
      stepUp,
      invoke: (intent, grant, signal) => api.commandProviderCostRate({
        command,
        expectedHeadRevision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      }),
      feedback: (response) => response.idempotent_replay
        ? "原成本率命令结果已安全恢复。"
        : "Provider 成本率版本已更新。",
      reload: loadRates,
    });
  }

  async function mutate({
    kind, fingerprint, actionCode, resourceType, resourceId, stepUp, invoke, feedback, reload,
  }) {
    const previous = store.snapshot().intents[kind];
    const intent = previous?.fingerprint === fingerprint
      ? previous
      : Object.freeze({ fingerprint, idempotencyKey: newIdempotencyKey(kind) });
    store.setIntent(kind, intent);
    const ticket = store.begin("write-" + kind);
    let grant = "";
    try {
      grant = await createStepUp({
        actionCode,
        resourceType,
        resourceId,
        stepUp,
        signal: ticket.controller.signal,
      });
      if (!grant || !store.isCurrent(ticket)) return false;
      const response = await invoke(intent, grant, ticket.controller.signal);
      grant = "";
      if (!store.accept(ticket, {})) return false;
      store.setIntent(kind, null);
      store.setFeedback(feedback(response));
      await reload();
      return true;
    } catch (error) {
      grant = "";
      if (!(error instanceof AuthApiError)
        || ![0, 503].includes(Number(error.status) || 0)) store.setIntent(kind, null);
      return handleFailure(ticket, error);
    }
  }

  async function createStepUp({ actionCode, resourceType, resourceId, stepUp, signal }) {
    const workspaceId = workspaceSession?.context?.workspace_id;
    if (!workspaceId || typeof api.createIdentityStepUpGrant !== "function") return "";
    const method = String(stepUp?.method ?? "totp");
    let code = String(stepUp?.verificationCode ?? "");
    let credential = null;
    let challengeId;
    try {
      if (method === "webauthn") {
        const challenge = await api.beginIdentityStepUp({ signal });
        challengeId = challenge.challenge_id;
        credential = await api.getWebAuthnAssertion(challenge.public_key_options);
      }
      const response = await api.createIdentityStepUpGrant({
        method,
        workspaceId,
        actionCode,
        resourceType,
        resourceId,
        code,
        challengeId,
        credential,
        signal,
      });
      return String(response.grant ?? "");
    } finally {
      code = "";
      credential = null;
    }
  }

  function handleFailure(ticket, error) {
    if (!store.reject(ticket, error)) return false;
    if (Number(error?.status) === 401) onSessionInvalid(error);
    if (Number(error?.status) === 403) onPermissionRevoked(error);
    return false;
  }

  return Object.freeze({
    open,
    close,
    refresh,
    loadSubscription,
    loadRates,
    configureRates,
    moveRatePage,
    submitDispute,
    applyRate,
    applyFxRate,
    snapshot: store.snapshot,
  });
}

function rateResourceId(command) {
  if (command?.action === "disable") {
    return String(command.workspace_provider_cost_rate_id ?? "");
  }
  return [
    String(command?.provider_id ?? ""),
    String(command?.model_id ?? ""),
    String(command?.currency ?? ""),
  ].join(":");
}

function isSubscriptionNavigation(value) {
  return value === "workspace-subscription" || value === "billing-disputes";
}

function isRateNavigation(value) {
  return value === "internal-finops" || value === "model-prices";
}

function compactObject(value) {
  return Object.fromEntries(Object.entries(value ?? {}).filter(([, item]) => (
    item !== "" && item !== null && item !== undefined
  )));
}

function defaultIdempotencyKey(kind = "command") {
  const crypto = globalThis.crypto;
  if (!crypto || typeof crypto.getRandomValues !== "function") {
    throw new AuthApiError({
      code: "workspace.configuration_invalid",
      message: "安全请求标识不可用。",
    });
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const suffix = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return ["commercial", String(kind), suffix].join(":");
}
