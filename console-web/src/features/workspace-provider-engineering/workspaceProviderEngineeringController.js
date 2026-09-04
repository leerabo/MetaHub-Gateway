/** Workspace Cookie controller for PRD-143 Provider compatibility and overrides. */

import { AuthApiError } from "../../lib/auth.js";
import { createWorkspaceProviderEngineeringStore } from "./workspaceProviderEngineeringStore.js";

export function createWorkspaceProviderEngineeringController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
  newIdempotencyKey = defaultIdempotencyKey,
} = {}) {
  if (!api) throw new TypeError("workspace provider-engineering controller requires api");
  const store = createWorkspaceProviderEngineeringStore({ onChange });
  let activation = 0;
  let activationController = null;
  let activationContext = null;
  let workspaceSession = null;

  async function open({ authContext, activeNavigationItemId, providerId }) {
    const currentActivation = ++activation;
    activationController?.abort();
    activationController = new AbortController();
    activationContext = { authContext, activeNavigationItemId, providerId };
    workspaceSession = null;
    store.prepare(providerId, activeNavigationItemId);
    try {
      const session = await api.workspaceSession({ signal: activationController.signal });
      if (currentActivation !== activation || activationController.signal.aborted) return false;
      if (!store.enter({ ...activationContext, workspaceSession: session })) return false;
      workspaceSession = session;
      return load();
    } catch (error) {
      if (error?.name === "AbortError" || currentActivation !== activation) return false;
      store.activationFailed(providerId, error, activeNavigationItemId);
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
    return load();
  }

  async function configure(filters = {}) {
    if (!workspaceSession) return false;
    store.configure(filters);
    return load();
  }

  async function nextPage() {
    if (!store.nextPage()) return false;
    return load();
  }

  async function previousPage() {
    if (!store.previousPage()) return false;
    return load();
  }

  async function load() {
    const snapshot = store.snapshot();
    if (!workspaceSession) return false;
    const ticket = store.begin("read");
    try {
      const response = await api.providerErrorOverrides({
        filters: {
          ...(snapshot.providerId ? { provider_id: snapshot.providerId } : {}),
          ...snapshot.filters,
        },
        cursor: snapshot.cursor,
        limit: 50,
        signal: ticket.controller.signal,
      });
      if (snapshot.providerId
        && (response.compatibility?.provider_id !== snapshot.providerId
          || response.overrides.some(
            (item) => item.provider_id !== null && item.provider_id !== snapshot.providerId,
          ))) {
        throw invalidOwner();
      }
      return store.accept(ticket, { response });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function apply(command, stepUp) {
    const snapshot = store.snapshot();
    if (!workspaceSession || !snapshot.canManage || !snapshot.response) return false;
    const resourceId = command.action === "create"
      ? command.provider_id
      : command.provider_error_override_id;
    if (command.action === "create" && resourceId !== snapshot.providerId) return false;
    const fingerprint = JSON.stringify({
      command,
      expectedHeadRevision: snapshot.response.head.revision,
    });
    const previous = snapshot.intent;
    const intent = previous?.fingerprint === fingerprint
      ? previous
      : Object.freeze({ fingerprint, idempotencyKey: newIdempotencyKey(command.action) });
    store.setIntent(intent);
    const ticket = store.begin("write");
    let grant = "";
    try {
      grant = await createStepUp({
        action: command.action,
        resourceId,
        stepUp,
        signal: ticket.controller.signal,
      });
      if (!grant || !store.isCurrent(ticket)) return false;
      const response = await api.commandProviderErrorOverride({
        command,
        expectedHeadRevision: snapshot.response.head.revision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal: ticket.controller.signal,
      });
      grant = "";
      if (response.override.provider_id !== response.compatibility.provider_id
        || (snapshot.providerId
          && response.compatibility.provider_id !== snapshot.providerId)) throw invalidOwner();
      if (!store.accept(ticket, { response: mergeResponse(snapshot.response, response) })) {
        return false;
      }
      store.setIntent(null);
      store.setFeedback(response.idempotent_replay
        ? "The original override result was restored safely."
        : response.changed
          ? "Provider error override updated."
          : "No effective override change was required.");
      await load();
      return true;
    } catch (error) {
      grant = "";
      if (!(error instanceof AuthApiError) || ![0, 503].includes(Number(error.status) || 0)) {
        store.setIntent(null);
      }
      return handleFailure(ticket, error);
    }
  }

  async function createStepUp({ action, resourceId, stepUp, signal }) {
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
        actionCode: `provider_error_override.${action}`,
        resourceType: "provider_error_override",
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
    if (Number(error?.status) === 401) {
      onSessionInvalid(error);
    } else if (Number(error?.status) === 403) {
      onPermissionRevoked(error);
    }
    return false;
  }

  return Object.freeze({
    open,
    close,
    refresh,
    configure,
    nextPage,
    previousPage,
    apply,
    snapshot: store.snapshot,
  });
}

function mergeResponse(current, command) {
  const items = [...current.overrides];
  const index = items.findIndex(
    (item) => item.provider_error_override_id === command.override.provider_error_override_id,
  );
  if (index < 0) items.unshift(command.override); else items[index] = command.override;
  return Object.freeze({
    ...current,
    overrides: Object.freeze(items),
    head: command.head,
    compatibility: command.compatibility,
    permission_revision: command.permission_revision,
    permission_fingerprint: command.permission_fingerprint,
    request_id: command.request_id,
    trace_id: command.trace_id,
  });
}

function invalidOwner() {
  return new AuthApiError({
    status: 502,
    code: "workspace.provider_engineering_response_invalid",
    message: "Provider detail response owner did not match the active Workspace.",
  });
}

function defaultIdempotencyKey(action = "command") {
  const crypto = globalThis.crypto;
  if (!crypto || typeof crypto.getRandomValues !== "function") {
    throw new AuthApiError({
      code: "workspace.configuration_invalid",
      message: "Secure request identifiers are unavailable.",
    });
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const suffix = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `provider-override:${String(action)}:${suffix}`;
}
