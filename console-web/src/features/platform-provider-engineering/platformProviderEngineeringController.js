/** Independent controller for the PRD-143 Platform Provider Engineering workbench. */

import { PlatformApiError } from "../../lib/platform.js";
import { createPlatformProviderEngineeringStore } from "./platformProviderEngineeringStore.js";

const RELEASE_ID = /^parl_[0-9a-f]{32}$/u;

export function createPlatformProviderEngineeringController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
  newIdempotencyKey = defaultIdempotencyKey,
} = {}) {
  if (!api) throw new TypeError("provider-engineering controller requires api");
  const store = createPlatformProviderEngineeringStore({ onChange });
  let context = null;

  async function loadAdapters() {
    const ticket = store.begin("adapters");
    const snapshot = store.snapshot();
    try {
      const adapters = await api.providerAdapters({
        filters: snapshot.filters,
        cursor: snapshot.adapterCursor,
        limit: 50,
        signal: ticket.controller.signal,
      });
      const previous = snapshot.selectedAdapterId;
      const selected = adapters.adapters.some((item) => item.provider_adapter_release_id === previous)
        ? previous
        : adapters.adapters[0]?.provider_adapter_release_id ?? null;
      if (!store.accept(ticket, {
        adapters,
        selectedAdapterId: selected,
        runs: selected === previous ? snapshot.runs : null,
        cases: selected === previous ? snapshot.cases : null,
        selectedRunId: selected === previous ? snapshot.selectedRunId : null,
      })) return false;
      if (selected && (!store.snapshot().runs || selected !== previous)) await loadRuns();
      return true;
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadRuns() {
    const adapterId = store.snapshot().selectedAdapterId;
    if (!RELEASE_ID.test(String(adapterId ?? ""))) return false;
    const ticket = store.begin("runs");
    const snapshot = store.snapshot();
    try {
      const runs = await api.adapterContracts({
        providerAdapterReleaseId: adapterId,
        view: "runs",
        filters: snapshot.runFilters,
        cursor: snapshot.runCursor,
        limit: 50,
        signal: ticket.controller.signal,
      });
      if (runs.provider_adapter_release_id !== adapterId) throw invalidOwner();
      const previous = snapshot.selectedRunId;
      const selected = runs.runs.some((item) => item.contract_run_id === previous)
        ? previous
        : null;
      return store.accept(ticket, {
        runs,
        selectedRunId: selected,
        cases: selected === previous ? snapshot.cases : null,
      });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadCases() {
    const snapshot = store.snapshot();
    if (!RELEASE_ID.test(String(snapshot.selectedAdapterId ?? "")) || !snapshot.selectedRunId) {
      return false;
    }
    const ticket = store.begin("cases");
    try {
      const cases = await api.adapterContracts({
        providerAdapterReleaseId: snapshot.selectedAdapterId,
        view: "cases",
        runId: snapshot.selectedRunId,
        cursor: snapshot.caseCursor,
        limit: 100,
        signal: ticket.controller.signal,
      });
      if (cases.provider_adapter_release_id !== snapshot.selectedAdapterId
        || cases.cases.some((item) => item.contract_run_id !== snapshot.selectedRunId)) {
        throw invalidOwner();
      }
      return store.accept(ticket, { cases });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  function enter(nextContext, navigationItems) {
    const accepted = store.enter(nextContext, navigationItems);
    context = accepted ? nextContext : null;
    return accepted;
  }

  function leave() {
    context = null;
    store.leave();
  }

  async function configureAdapters(filters) {
    store.configureAdapters(filters);
    return loadAdapters();
  }

  async function configureRuns(filters) {
    store.setRunFilters(filters);
    return loadRuns();
  }

  async function selectAdapter(adapterId) {
    if (!store.selectAdapter(adapterId)) return false;
    return loadRuns();
  }

  async function selectRun(runId) {
    if (!store.selectRun(runId)) return false;
    return loadCases();
  }

  async function nextPage(kind) {
    if (!store.nextPage(kind)) return false;
    return kind === "adapters" ? loadAdapters() : kind === "runs" ? loadRuns() : loadCases();
  }

  async function previousPage(kind) {
    if (!store.previousPage(kind)) return false;
    return kind === "adapters" ? loadAdapters() : kind === "runs" ? loadRuns() : loadCases();
  }

  async function applyAdapter({ adapterIdentifier, command, expectedRevision }, stepUp) {
    if (!adapterPermission(command?.action)) return false;
    return mutate({
      kind: "adapter",
      fingerprint: JSON.stringify({ adapterIdentifier, command, expectedRevision }),
      actionCode: `provider_adapter.${command.action}`,
      resourceType: "provider_adapter_release",
      resourceId: adapterIdentifier,
      stepUp,
      invoke: (intent, grant, signal) => api.commandProviderAdapter({
        adapterIdentifier,
        command,
        expectedRevision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      }),
      accept: (response) => ({
        selectedAdapterId: response.adapter.provider_adapter_release_id,
        runs: command.action === "run_contract" ? null : store.snapshot().runs,
        cases: command.action === "run_contract" ? null : store.snapshot().cases,
      }),
      after: async (response) => {
        store.setFeedback(response.idempotent_replay
          ? "Adapter 命令已按原请求安全重放。"
          : command.action === "run_contract"
            ? "合同运行已进入固定执行队列。"
            : "Adapter 状态已更新。");
        await loadAdapters();
      },
    });
  }

  async function applyCatalog({ command, expectedRevision }, stepUp) {
    if (!catalogPermission(command?.action)) return false;
    const resourceId = command.action === "create_candidate"
      ? command.builtin_catalog_version
      : command.error_catalog_release_id;
    return mutate({
      kind: "catalog",
      fingerprint: JSON.stringify({ command, expectedRevision }),
      actionCode: `provider_error_catalog.${command.action}`,
      resourceType: "provider_error_catalog_release",
      resourceId,
      stepUp,
      invoke: (intent, grant, signal) => api.commandErrorCatalog({
        command,
        expectedRevision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      }),
      accept: (response) => ({ catalog: response.catalog }),
      after: async (response) => {
        store.setFeedback(response.idempotent_replay
          ? "错误目录命令已按原请求安全重放。" : "内置错误目录状态已更新。");
      },
    });
  }

  async function mutate({
    kind, fingerprint, actionCode, resourceType, resourceId, stepUp, invoke, accept, after,
  }) {
    const previous = store.snapshot().intents[kind];
    const intent = previous?.fingerprint === fingerprint
      ? previous
      : Object.freeze({ fingerprint, idempotencyKey: newIdempotencyKey(kind) });
    store.setIntent(kind, intent);
    const ticket = store.begin(`write-${kind}`);
    let grant = "";
    try {
      grant = await createStepUp({ actionCode, resourceType, resourceId, stepUp, signal: ticket.controller.signal });
      if (!grant || !store.isCurrent(ticket)) return false;
      const response = await invoke(intent, grant, ticket.controller.signal);
      grant = "";
      if (!store.accept(ticket, accept(response))) return false;
      store.setIntent(kind, null);
      await after(response);
      return true;
    } catch (error) {
      grant = "";
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(Number(error.status) || 0)) {
        store.setIntent(kind, null);
      }
      return handleFailure(ticket, error);
    }
  }

  async function createStepUp({ actionCode, resourceType, resourceId, stepUp, signal }) {
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

  function adapterPermission(action) {
    const permission = {
      create_candidate: "platform.provider_adapter.candidate_manage",
      run_contract: "platform.provider_adapter.contract_run",
      review_failure: "platform.provider_adapter.failure_review",
      release: "platform.provider_adapter.release",
      deprecate: "platform.provider_adapter.release",
      revoke: "platform.provider_adapter.release",
    }[String(action ?? "")];
    return Boolean(permission && currentPermissions().has(permission));
  }

  function catalogPermission(action) {
    const permission = {
      create_candidate: "platform.error_catalog.candidate_manage",
      submit_review: "platform.error_catalog.candidate_manage",
      security_approve: "platform.error_catalog.security_approve",
      release: "platform.error_catalog.release",
      deprecate: "platform.error_catalog.release",
    }[String(action ?? "")];
    return Boolean(permission && currentPermissions().has(permission));
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
    loadAdapters,
    loadRuns,
    loadCases,
    configureAdapters,
    configureRuns,
    selectAdapter,
    selectRun,
    nextPage,
    previousPage,
    applyAdapter,
    applyCatalog,
    snapshot: store.snapshot,
  });
}
function invalidOwner() {
  return new PlatformApiError({
    status: 502,
    code: "platform.provider_engineering_response_invalid",
    message: "Provider Engineering 响应 owner 不一致。",
  });
}

function defaultIdempotencyKey(kind = "command") {
  const crypto = globalThis.crypto;
  if (!crypto || typeof crypto.getRandomValues !== "function") {
    throw new PlatformApiError({ code: "platform.configuration_invalid", message: "当前环境无法生成安全请求标识。" });
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `provider-engineering:${kind}:${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}
