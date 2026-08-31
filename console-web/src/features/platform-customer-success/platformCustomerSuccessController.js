/** Independent controller for the PRD-140 customer-success workbench. */

import { PlatformApiError } from "../../lib/platform.js";
import { createPlatformCustomerSuccessStore } from "./platformCustomerSuccessStore.js";

const ACCOUNT_ID = /^csa_[0-9a-f]{32}$/u;

export function createPlatformCustomerSuccessController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
  newIdempotencyKey = defaultIdempotencyKey,
} = {}) {
  if (!api) throw new TypeError("customer-success controller requires api");
  const store = createPlatformCustomerSuccessStore({ onChange });
  let context = null;

  async function loadDirectory() {
    const ticket = store.begin("directory");
    const query = store.snapshot();
    try {
      const directory = await api.customerAccounts({
        filters: query.filters,
        cursor: query.directoryCursor,
        limit: 50,
        signal: ticket.controller.signal,
      });
      const previous = query.selectedAccountId;
      const selected = directory.items.some((item) => item.customer_account_id === previous)
        ? previous
        : directory.items[0]?.customer_account_id ?? null;
      if (!store.accept(ticket, {
        directory,
        selectedAccountId: selected,
        detail: selected === previous ? query.detail : null,
        health: selected === previous ? query.health : null,
        renewals: selected === previous ? query.renewals : null,
        contacts: null,
        contactsVisible: false,
      })) return false;
      if (selected) await loadSelected();
      return true;
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadSelected() {
    const accountId = store.snapshot().selectedAccountId;
    if (!ACCOUNT_ID.test(String(accountId ?? ""))) return false;
    const permissions = currentPermissions();
    const tasks = [];
    if (permissions.has("platform.customer_success.read")) tasks.push(loadDetail(accountId));
    if (permissions.has("platform.customer_success.read")
      || permissions.has("platform.customer_success.support_read")) {
      tasks.push(loadHealth(accountId));
    }
    if (permissions.has("platform.customer_success.renewal.read")) {
      tasks.push(loadRenewals(accountId));
    }
    const results = await Promise.all(tasks);
    return results.some(Boolean);
  }

  async function loadDetail(accountId = store.snapshot().selectedAccountId) {
    if (!currentPermissions().has("platform.customer_success.read")) return false;
    const ticket = store.begin("detail");
    try {
      const response = await api.customerAccounts({
        filters: { customer_account_id: accountId, view: "detail" },
        limit: 1,
        signal: ticket.controller.signal,
      });
      if (!response.items.some((item) => item.customer_account_id === accountId)
        || response.detail === null) throw invalidOwner();
      return store.accept(ticket, { detail: response.detail });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadHealth(accountId = store.snapshot().selectedAccountId) {
    const ticket = store.begin("health");
    try {
      const health = await api.customerHealth({
        customerAccountId: accountId,
        signal: ticket.controller.signal,
      });
      if (health.customer_account_id !== accountId) throw invalidOwner();
      return store.accept(ticket, { health });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadRenewals(accountId = store.snapshot().selectedAccountId) {
    const ticket = store.begin("renewals");
    try {
      const renewals = await api.customerRenewals({
        filters: { customer_account_id: accountId },
        limit: 50,
        signal: ticket.controller.signal,
      });
      if (renewals.items.some((item) => item.customer_account_id !== accountId)) {
        throw invalidOwner();
      }
      return store.accept(ticket, { renewals });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function loadContacts() {
    const accountId = store.snapshot().selectedAccountId;
    if (!currentPermissions().has("platform.customer_success.contact.read")
      || !ACCOUNT_ID.test(String(accountId ?? ""))) return false;
    const ticket = store.begin("contacts");
    try {
      const response = await api.customerAccounts({
        filters: { customer_account_id: accountId, view: "contact" },
        limit: 1,
        signal: ticket.controller.signal,
      });
      if (!response.items.some((item) => item.customer_account_id === accountId)
        || response.contacts === null) throw invalidOwner();
      return store.accept(ticket, { contacts: response.contacts, contactsVisible: true });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function applyPlan(command) {
    if (!currentPermissions().has("platform.customer_success.plan.manage")) return false;
    return mutate("plan", command, (intent, signal) => api.customerPlan({
      command,
      idempotencyKey: intent.idempotencyKey,
      signal,
    }), async (response) => {
      store.setFeedback(response.idempotent_replay
        ? "计划命令已按原请求安全重放。" : "客户成功计划已更新。");
      await loadDetail(response.plan.customer_account_id);
    });
  }

  async function applyTask(command, stepUp = null) {
    if (!currentPermissions().has("platform.customer_success.task.manage")) return false;
    return mutate("task", command, async (intent, signal, ticket) => {
      let grant = null;
      if (command.operation === "approve_batch") {
        grant = await createStepUp(stepUp, command.customer_task_batch_id, signal);
        if (!store.isCurrent(ticket) || !grant) throw abortError();
      }
      try {
        return await api.customerTask({
          command,
          idempotencyKey: intent.idempotencyKey,
          stepUpGrant: grant,
          signal,
        });
      } finally {
        grant = null;
      }
    }, async (response) => {
      store.setFeedback(response.idempotent_replay
        ? "任务命令已按原请求安全重放。"
        : response.batch?.status === "awaiting_approval"
          ? "批量任务已提交独立审批。"
          : response.batch?.status === "rejected"
            ? "批量任务已拒绝。"
            : "客户任务已更新。");
      const accountId = response.task?.customer_account_id ?? store.snapshot().selectedAccountId;
      if (accountId) await loadDetail(accountId);
    }, responsePatchForTask);
  }

  async function mutate(kind, command, operation, afterSuccess, responsePatch = () => ({})) {
    const fingerprint = JSON.stringify(command);
    const previous = store.snapshot().intents[kind];
    const intent = previous?.fingerprint === fingerprint
      ? previous
      : Object.freeze({ fingerprint, idempotencyKey: newIdempotencyKey() });
    store.setIntent(kind, intent);
    const ticket = store.begin(`write-${kind}`);
    try {
      const response = await operation(intent, ticket.controller.signal, ticket);
      if (!store.accept(ticket, responsePatch(response))) return false;
      store.setIntent(kind, null);
      await afterSuccess(response);
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        store.setIntent(kind, null);
      }
      return handleFailure(ticket, error);
    }
  }

  async function createStepUp(input, batchId, signal) {
    const method = String(input?.method ?? "totp");
    let code = String(input?.verificationCode ?? "");
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
        actionCode: "platform.customer_success.batch.approve",
        resourceType: "customer_task_batch",
        resourceId: batchId,
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

  function enter(nextContext, navigationItems) {
    const accepted = store.enter(nextContext, navigationItems);
    context = accepted ? nextContext : null;
    return accepted;
  }

  function leave() {
    context = null;
    store.leave();
  }

  function selectAccount(accountId) {
    if (!store.selectAccount(accountId)) return false;
    void loadSelected();
    return true;
  }

  function selectTab(tab) {
    return store.selectTab(tab);
  }

  async function configureDirectory(filters) {
    store.configureDirectory(filters);
    return loadDirectory();
  }

  async function nextPage() {
    return store.nextDirectoryPage() ? loadDirectory() : false;
  }

  async function previousPage() {
    return store.previousDirectoryPage() ? loadDirectory() : false;
  }

  function currentPermissions() {
    return new Set(context?.permissions ?? []);
  }

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
    loadDirectory,
    loadSelected,
    loadContacts,
    hideContacts: store.hideContacts,
    configureDirectory,
    nextPage,
    previousPage,
    selectAccount,
    selectTab,
    applyPlan,
    applyTask,
    snapshot: store.snapshot,
  });
}

function responsePatchForTask(response) {
  return response.batch ? { batch: response.batch } : {};
}

function invalidOwner() {
  return new PlatformApiError({
    status: 502,
    code: "platform.customer_success_response_invalid",
    message: "客户成功响应的 owner 不一致。",
  });
}

function abortError() {
  return new DOMException("Operation aborted", "AbortError");
}

function defaultIdempotencyKey() {
  const crypto = globalThis.crypto;
  if (!crypto || typeof crypto.getRandomValues !== "function") {
    throw new PlatformApiError({
      code: "platform.configuration_invalid",
      message: "当前环境无法生成安全请求标识。",
    });
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `customer-success:${Array.from(
    bytes,
    (value) => value.toString(16).padStart(2, "0"),
  ).join("")}`;
}
