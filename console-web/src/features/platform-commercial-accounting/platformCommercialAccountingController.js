/** Independent controller for the PRD-144 Platform commercial accounting domain. */

import { PlatformApiError } from "../../lib/platform.js";
import { createPlatformCommercialAccountingStore } from "./platformCommercialAccountingStore.js";

export function createPlatformCommercialAccountingController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
  newIdempotencyKey = defaultIdempotencyKey,
} = {}) {
  if (!api) throw new TypeError("commercial-accounting controller requires api");
  const store = createPlatformCommercialAccountingStore({ onChange });
  let context = null;

  function enter(nextContext, navigationItems) {
    const accepted = store.enter(nextContext, navigationItems);
    context = accepted ? nextContext : null;
    return accepted;
  }

  function leave() {
    context = null;
    store.leave();
  }

  async function refresh() {
    const permissions = currentPermissions();
    const operations = [];
    if (permissions.has("platform.commercial.plan.read")) operations.push(loadPlans());
    if (permissions.has("platform.commercial.subscription.read")) {
      operations.push(loadSubscriptions());
    }
    if (!operations.length) return false;
    const results = await Promise.all(operations);
    return results.some(Boolean);
  }

  async function loadPlans() {
    if (!currentPermissions().has("platform.commercial.plan.read")) return false;
    const ticket = store.begin("plans");
    const snapshot = store.snapshot();
    try {
      const plans = await api.commercialPlans({
        filters: compactObject(snapshot.planFilters),
        cursor: snapshot.planCursor,
        limit: 50,
        signal: ticket.controller.signal,
      });
      return store.accept(ticket, { plans });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadSubscriptions() {
    if (!currentPermissions().has("platform.commercial.subscription.read")) return false;
    const ticket = store.begin("subscriptions");
    const snapshot = store.snapshot();
    try {
      const subscriptions = await api.commercialSubscriptions({
        filters: compactObject(snapshot.subscriptionFilters),
        cursor: snapshot.subscriptionCursor,
        limit: 50,
        signal: ticket.controller.signal,
      });
      return store.accept(ticket, { subscriptions });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function configure(kind, filters) {
    if (!store.configure(kind, filters)) return false;
    return kind === "plans" ? loadPlans() : loadSubscriptions();
  }

  async function movePage(kind, direction) {
    const moved = direction === "next" ? store.nextPage(kind) : store.previousPage(kind);
    if (!moved) return false;
    return kind === "plans" ? loadPlans() : loadSubscriptions();
  }

  async function applyPlan({ command, expectedRevision, resourceId }, stepUp) {
    if (!currentPermissions().has("platform.commercial.plan.manage")) return false;
    const id = command?.action === "create_candidate"
      ? command.plan_code
      : String(resourceId ?? "");
    return mutate({
      kind: "plan",
      fingerprint: JSON.stringify({ command, expectedRevision, resourceId: id }),
      actionCode: `commercial_plan.${String(command?.action ?? "")}`,
      resourceType: command?.action === "create_candidate"
        ? "platform_plan_catalog"
        : "platform_plan_catalog_release",
      resourceId: id,
      stepUp,
      invoke: (intent, grant, signal) => api.commandCommercialPlan({
        command,
        expectedRevision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      }),
      after: async (response) => {
        store.setFeedback(response.idempotent_replay
          ? "套餐命令已按原请求安全重放。"
          : "套餐 release 状态已更新。");
        await loadPlans();
      },
    });
  }

  async function applyReferenceRate({
    providerId, command, expectedRevision, resourceId,
  }, stepUp) {
    if (!currentPermissions().has("platform.commercial.plan.manage")) return false;
    const id = command?.action === "create_candidate"
      ? `${providerId}:${command.model_id}:${command.currency}`
      : String(resourceId ?? command?.provider_reference_rate_release_id ?? "");
    return mutate({
      kind: "reference",
      fingerprint: JSON.stringify({
        providerId, command, expectedRevision, resourceId: id,
      }),
      actionCode: `provider_reference_rate.${String(command?.action ?? "")}`,
      resourceType: "platform_provider_reference_rate",
      resourceId: id,
      stepUp,
      invoke: (intent, grant, signal) => api.commandProviderReferenceRate({
        providerId,
        command,
        expectedRevision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      }),
      after: async (response) => {
        store.setFeedback(response.idempotent_replay
          ? "参考成本命令已按原请求安全重放。"
          : "官方参考成本 release 状态已更新。");
        await loadPlans();
      },
    });
  }

  async function decideDispute({ disputeId, command, expectedRevision }, stepUp) {
    if (!currentPermissions().has("platform.commercial.dispute.decide")) return false;
    return mutate({
      kind: "dispute",
      fingerprint: JSON.stringify({ disputeId, command, expectedRevision }),
      actionCode: `commercial_dispute.${String(command?.action ?? "")}`,
      resourceType: "billing_dispute_case",
      resourceId: disputeId,
      stepUp,
      invoke: (intent, grant, signal) => api.decideCommercialDispute({
        disputeId,
        command,
        expectedRevision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      }),
      after: async (response) => {
        store.setFeedback(response.idempotent_replay
          ? "争议裁决已按原请求安全重放。"
          : "商业争议状态已更新。");
        await loadSubscriptions();
      },
    });
  }

  async function mutate({
    kind, fingerprint, actionCode, resourceType, resourceId, stepUp, invoke, after,
  }) {
    const previous = store.snapshot().intents[kind];
    const intent = previous?.fingerprint === fingerprint
      ? previous
      : Object.freeze({ fingerprint, idempotencyKey: newIdempotencyKey(kind) });
    store.setIntent(kind, intent);
    const ticket = store.begin(`write-${kind}`);
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
      await after(response);
      return true;
    } catch (error) {
      grant = "";
      if (!(error instanceof PlatformApiError)
        || ![0, 503].includes(Number(error.status) || 0)) store.setIntent(kind, null);
      return handleFailure(ticket, error);
    }
  }

  async function createStepUp({
    actionCode, resourceType, resourceId, stepUp, signal,
  }) {
    if (typeof api.createIdentityStepUpGrant !== "function") return "";
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

  function currentPermissions() { return new Set(context?.permissions ?? []); }

  function handleFailure(ticket, error) {
    if (!store.reject(ticket, error)) return false;
    if (error instanceof PlatformApiError && error.status === 401) {
      leave();
      onSessionInvalid(error);
    } else if (error instanceof PlatformApiError && error.status === 403) {
      onPermissionRevoked(error);
    }
    return false;
  }

  return Object.freeze({
    enter,
    leave,
    refresh,
    loadPlans,
    loadSubscriptions,
    configure,
    movePage,
    applyPlan,
    applyReferenceRate,
    decideDispute,
    snapshot: store.snapshot,
  });
}

function compactObject(value) {
  return Object.fromEntries(
    Object.entries(value ?? {}).filter(([, item]) => (
      item !== "" && item !== null && item !== undefined
    )),
  );
}

function defaultIdempotencyKey(kind = "command") {
  const crypto = globalThis.crypto;
  if (!crypto || typeof crypto.getRandomValues !== "function") {
    throw new PlatformApiError({
      code: "platform.configuration_invalid",
      message: "当前环境无法生成安全请求标识。",
    });
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const suffix = Array.from(
    bytes, (value) => value.toString(16).padStart(2, "0"),
  ).join("");
  return `commercial-accounting:${String(kind)}:${suffix}`;
}
