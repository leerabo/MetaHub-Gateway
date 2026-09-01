/** Independent controller for the PRD-141 Platform Growth workbench. */

import { PlatformApiError } from "../../lib/platform.js";
import { createPlatformGrowthStore } from "./platformGrowthStore.js";

const CAMPAIGN_ID = /^gca_[0-9a-f]{32}$/u;
const EXPERIMENT_ID = /^gex_[0-9a-f]{32}$/u;
const EXPORT_ID = /^gxp_[0-9a-f]{32}$/u;

export function createPlatformGrowthController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
  newIdempotencyKey = defaultIdempotencyKey,
  saveDownload = defaultSaveDownload,
} = {}) {
  if (!api) throw new TypeError("growth controller requires api");
  const store = createPlatformGrowthStore({ onChange });
  let context = null;

  async function load(tab = store.snapshot().tab) {
    const name = String(tab ?? "");
    const snapshot = store.snapshot();
    if (!snapshot.availableTabs.includes(name)) return false;
    const operation = readOperation(name);
    if (!operation) return false;
    const ticket = store.begin(`read-${name}`);
    const query = store.snapshot();
    try {
      const response = await operation({
        filters: scopedFilters(name, query.filters[name]),
        cursor: query.cursors[name],
        limit: ["campaigns", "experiments"].includes(name) ? 50 : 100,
        signal: ticket.controller.signal,
      });
      return store.accept(ticket, (current) => ({
        data: Object.freeze({ ...current.data, [name]: response }),
      }));
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  function readOperation(tab) {
    return {
      funnels: api.growthFunnels,
      cohorts: api.growthCohorts,
      campaigns: api.growthCampaigns,
      experiments: api.growthExperiments,
      attribution: api.growthAttribution,
    }[tab] ?? null;
  }

  function scopedFilters(tab, filters) {
    const result = compactObject(filters);
    const permissions = currentPermissions();
    if (!permissions.has("platform.growth.read")
      && permissions.has("platform.growth.adoption_read")) {
      if (tab === "funnels" && !["adoption", "retention"].includes(result.view)) {
        result.view = "adoption";
      }
      if (tab === "cohorts" && !["adoption", "retention"].includes(result.view)) {
        result.view = "retention";
      }
      delete result.dimension_value;
      if (["source", "channel"].includes(result.dimension)) result.dimension = "none";
    }
    if (tab === "attribution"
      && permissions.has("platform.growth.channel_attribution_read")
      && !permissions.has("platform.growth.read")) {
      return { view: "channel_summary" };
    }
    return result;
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

  function selectTab(tab) {
    if (!store.selectTab(tab)) return false;
    if (store.snapshot().data[tab] === null) void load(tab);
    return true;
  }

  async function configure(tab, filters) {
    if (!store.configure(tab, compactObject(filters))) return false;
    return load(tab);
  }

  async function nextPage(tab = store.snapshot().tab) {
    return store.nextPage(tab) ? load(tab) : false;
  }

  async function previousPage(tab = store.snapshot().tab) {
    return store.previousPage(tab) ? load(tab) : false;
  }

  async function applyCampaign(command, stepUp) {
    if (!currentPermissions().has("platform.growth.campaign.manage")
      || typeof api.growthCampaignCommand !== "function") return false;
    return mutate("campaign", command, async (intent, signal, ticket) => {
      const grant = await createStepUp({
        ...stepUp,
        actionCode: `platform.growth.${command.operation}`,
        resourceType: "growth_campaign",
        resourceId: command.campaign_id ?? "new",
        ticket,
      });
      if (!grant || !store.isCurrent(ticket)) throw abortError();
      return api.growthCampaignCommand({
        command,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      });
    }, async (response) => {
      store.setFeedback(response.idempotent_replay
        ? "活动命令已按原请求安全重放。" : "活动状态已更新。");
      await load("campaigns");
    });
  }

  async function applyExperiment(command, stepUp) {
    if (!currentPermissions().has("platform.growth.experiment.manage")
      || typeof api.growthExperimentCommand !== "function") return false;
    return mutate("experiment", command, async (intent, signal, ticket) => {
      const grant = await createStepUp({
        ...stepUp,
        actionCode: `platform.growth.${command.operation}`,
        resourceType: "growth_experiment",
        resourceId: command.experiment_id ?? "new",
        ticket,
      });
      if (!grant || !store.isCurrent(ticket)) throw abortError();
      return api.growthExperimentCommand({
        command,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      });
    }, async (response) => {
      store.setFeedback(response.idempotent_replay
        ? "实验命令已按原请求安全重放。" : "实验状态已更新。");
      await load("experiments");
    });
  }

  async function requestExport(manifest, stepUp) {
    if (!currentPermissions().has("platform.growth.export.request")
      || typeof api.growthRequestExport !== "function") return false;
    return mutate("export", manifest, async (intent, signal, ticket) => {
      const grant = await createStepUp({
        ...stepUp,
        actionCode: "platform.growth.export.request",
        resourceType: "growth_export",
        resourceId: "new",
        ticket,
      });
      if (!grant || !store.isCurrent(ticket)) throw abortError();
      return api.growthRequestExport({
        manifest,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      });
    }, async (response) => {
      store.setFeedback(response.idempotent_replay
        ? "导出申请已按原请求安全重放。" : "导出申请已提交。");
    }, (response) => ({ export: response.export }));
  }

  async function loadExport(exportId = store.snapshot().export?.export_id) {
    if (!EXPORT_ID.test(String(exportId ?? ""))
      || typeof api.growthExport !== "function") return false;
    const ticket = store.begin("read-export");
    try {
      const response = await api.growthExport({
        exportId,
        signal: ticket.controller.signal,
      });
      return store.accept(ticket, { export: response.export });
    } catch (error) {
      return handleFailure(ticket, error);
    }
  }

  async function decideExport(command, exportId, stepUp) {
    const id = String(exportId ?? store.snapshot().export?.export_id ?? "");
    if (!EXPORT_ID.test(id)
      || !currentPermissions().has("platform.growth.export.approve")
      || typeof api.growthApproveExport !== "function") return false;
    return mutate("approval", { ...command, export_id: id }, async (intent, signal, ticket) => {
      const grant = await createStepUp({
        ...stepUp,
        actionCode: `platform.growth.export.${command.operation}`,
        resourceType: "growth_export",
        resourceId: id,
        ticket,
      });
      if (!grant || !store.isCurrent(ticket)) throw abortError();
      return api.growthApproveExport({
        exportId: id,
        command,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal,
      });
    }, async (response) => {
      store.setFeedback(response.idempotent_replay
        ? "审批决定已按原请求安全重放。" : "导出审批已提交。");
    }, (response) => ({ export: response.export }));
  }

  async function downloadExport(exportId, stepUp) {
    const id = String(exportId ?? store.snapshot().export?.export_id ?? "");
    if (!EXPORT_ID.test(id)
      || !currentPermissions().has("platform.growth.export.download")
      || typeof api.growthExportReceipt !== "function"
      || typeof api.growthExportDownload !== "function") return false;
    const ticket = store.begin("write-download");
    let receipt = "";
    try {
      const grant = await createStepUp({
        ...stepUp,
        actionCode: "platform.growth.export.receipt",
        resourceType: "growth_export",
        resourceId: id,
        ticket,
      });
      if (!grant || !store.isCurrent(ticket)) throw abortError();
      const issued = await api.growthExportReceipt({
        exportId: id,
        stepUpGrant: grant,
        signal: ticket.controller.signal,
      });
      receipt = issued.receipt;
      if (!store.isCurrent(ticket)) throw abortError();
      const file = await api.growthExportDownload({
        exportId: id,
        receipt,
        signal: ticket.controller.signal,
      });
      receipt = "";
      if (!store.accept(ticket, {})) return false;
      saveDownload(file);
      store.setFeedback("导出文件已下载，一次性凭据已消费。");
      await loadExport(id);
      return true;
    } catch (error) {
      receipt = "";
      return handleFailure(ticket, error);
    }
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

  async function createStepUp({
    method = "totp", verificationCode = "", actionCode, resourceType, resourceId, ticket,
  }) {
    if (typeof api.createIdentityStepUpGrant !== "function") return "";
    let code = String(verificationCode ?? "");
    let credential = null;
    let challengeId;
    try {
      if (method === "webauthn") {
        if (typeof api.beginIdentityStepUp !== "function"
          || typeof api.getWebAuthnAssertion !== "function") return "";
        const challenge = await api.beginIdentityStepUp({ signal: ticket.controller.signal });
        if (!store.isCurrent(ticket)) return "";
        challengeId = challenge.challenge_id;
        credential = await api.getWebAuthnAssertion(challenge.public_key_options);
        if (!store.isCurrent(ticket)) return "";
      }
      const response = await api.createIdentityStepUpGrant({
        method,
        actionCode,
        resourceType,
        resourceId,
        code,
        challengeId,
        credential,
        signal: ticket.controller.signal,
      });
      return store.isCurrent(ticket) ? String(response.grant ?? "") : "";
    } finally {
      code = "";
      credential = null;
    }
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
    load,
    selectTab,
    configure,
    nextPage,
    previousPage,
    applyCampaign,
    applyExperiment,
    requestExport,
    loadExport,
    decideExport,
    downloadExport,
    snapshot: store.snapshot,
  });
}

function compactObject(value) {
  return Object.fromEntries(Object.entries(value ?? {}).filter(([, item]) => (
    item !== "" && item !== null && item !== undefined
  )));
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
  return `growth:${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function defaultSaveDownload({ blob, filename }) {
  const url = globalThis.URL?.createObjectURL?.(blob);
  if (!url || !globalThis.document?.createElement) return false;
  try {
    const anchor = globalThis.document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = "noopener";
    anchor.click();
    return true;
  } finally {
    globalThis.URL.revokeObjectURL(url);
  }
}

export function isPlatformGrowthObjectId(kind, value) {
  return { campaign: CAMPAIGN_ID, experiment: EXPERIMENT_ID, export: EXPORT_ID }[kind]
    ?.test(String(value ?? "")) ?? false;
}
