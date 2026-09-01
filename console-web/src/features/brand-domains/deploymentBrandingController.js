/** Workspace DeploymentBrand state. It is independent from Edge desired-state projections. */

import { AuthApiError } from "../../lib/auth.js";

export function createDeploymentBrandingController({
  api,
  newIdempotencyKey,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
} = {}) {
  if (!api || typeof newIdempotencyKey !== "function") throw new TypeError("deployment branding controller requires api");
  let ownerKey = "";
  let workspaceId = "";
  let canRead = false;
  let canManage = false;
  let deployments = new Map();
  let selectedDeploymentId = "";
  let lane = emptyLane();

  function enter(owner, visibleDeployments) {
    const key = ownerKeyFor(owner);
    if (!key) { clear(); return false; }
    if (ownerKey && ownerKey !== key) clear();
    ownerKey = key;
    canRead = owner.canReadBranding === true;
    canManage = owner.canManageBranding === true && canRead;
    deployments = new Map((visibleDeployments ?? []).map((item) => [
      String(item.deployment_id ?? ""), String(item.workspace_id ?? ""),
    ]).filter(([id, workspace]) => /^edp_[0-9a-f]{32}$/u.test(id) && /^wsp_[0-9a-f]{32}$/u.test(workspace)));
    if (!canRead || !deployments.has(selectedDeploymentId)) resetLane();
    workspaceId = selectedDeploymentId ? deployments.get(selectedDeploymentId) ?? "" : "";
    emit();
    return canRead;
  }

  async function select(deploymentId) {
    const id = String(deploymentId ?? "");
    if (!canRead || !deployments.has(id)) return false;
    if (selectedDeploymentId !== id) resetLane();
    selectedDeploymentId = id;
    workspaceId = deployments.get(id);
    emit();
    return load();
  }

  function close() { resetLane(); emit(); }

  async function load() {
    if (!ownerKey || !canRead || !selectedDeploymentId || typeof api.deploymentBrand !== "function") return false;
    const ticket = begin("loading");
    emit();
    try {
      const response = await api.deploymentBrand({
        deploymentId: selectedDeploymentId,
        signal: ticket.controller.signal,
      });
      if (!current(ticket)) return false;
      lane = accepted(lane, response.branding, null);
      emit();
      return true;
    } catch (error) {
      return reject(error, ticket);
    }
  }

  async function mutate(input) {
    const currentBrand = lane.branding;
    if (!canManage || !currentBrand || !currentBrand.actions.includes(input?.command?.action)
      || input?.confirmed !== true || !new Set(["totp", "webauthn"]).has(input?.method)
      || typeof api.updateDeploymentBrand !== "function") return false;
    const fingerprint = await commandFingerprint(
      currentBrand.deployment_brand_id, input.command, input.method,
    );
    const idempotencyKey = lane.intent?.fingerprint === fingerprint
      ? lane.intent.idempotencyKey : newIdempotencyKey("deployment-brand");
    lane = { ...lane, intent: { fingerprint, idempotencyKey } };
    const ticket = begin("ready", true);
    emit();
    let verificationCode = String(input.verificationCode ?? "");
    let stepUpGrant = "";
    try {
      stepUpGrant = await workspaceStepUp({
        method: input.method,
        verificationCode,
        action: input.command.action,
        brandId: currentBrand.deployment_brand_id,
        ticket,
      });
      verificationCode = "";
      if (!stepUpGrant || !current(ticket)) return false;
      const response = await api.updateDeploymentBrand({
        deploymentId: selectedDeploymentId,
        command: input.command,
        expectedRevision: currentBrand.revision,
        stepUpGrant,
        idempotencyKey,
        signal: ticket.controller.signal,
      });
      stepUpGrant = "";
      if (!current(ticket)) return false;
      lane = accepted(lane, response.branding, Object.freeze({
        message: actionMessage(input.command.action), requestId: response.request_id,
      }));
      emit();
      return true;
    } catch (error) {
      if (!(error instanceof AuthApiError) || ![0, 503].includes(error.status)) lane = { ...lane, intent: null };
      return reject(error, ticket);
    } finally {
      verificationCode = "";
      stepUpGrant = "";
    }
  }

  async function workspaceStepUp({ method, verificationCode, action, brandId, ticket }) {
    let challengeId;
    let credential = null;
    if (method === "webauthn") {
      const challenge = await api.beginIdentityStepUp({ signal: ticket.controller.signal });
      if (!current(ticket)) return "";
      challengeId = challenge.challenge_id;
      credential = await api.getWebAuthnAssertion(challenge.public_key_options);
      if (!current(ticket)) return "";
    }
    const response = await api.createIdentityStepUpGrant({
      method,
      workspaceId,
      actionCode: `brand.deployment.${action}`,
      resourceType: "deployment_brand",
      resourceId: brandId,
      code: verificationCode,
      challengeId,
      credential,
      signal: ticket.controller.signal,
    });
    credential = null;
    return current(ticket) ? String(response.grant ?? "") : "";
  }

  function begin(phase, busy = false) {
    lane.operation?.controller.abort();
    const ticket = Object.freeze({
      ownerKey,
      deploymentId: selectedDeploymentId,
      generation: lane.generation + 1,
      controller: new AbortController(),
    });
    lane = { ...lane, phase, busy, error: null, feedback: null, generation: ticket.generation, operation: ticket };
    return ticket;
  }

  function current(ticket) {
    return Boolean(ticket && lane.operation === ticket && ticket.ownerKey === ownerKey
      && ticket.deploymentId === selectedDeploymentId && ticket.generation === lane.generation
      && !ticket.controller.signal.aborted);
  }

  function reject(error, ticket) {
    if (!current(ticket) || error?.name === "AbortError") return false;
    if (error instanceof AuthApiError && error.status === 401) {
      clear();
      onSessionInvalid(error);
      return false;
    }
    if (error instanceof AuthApiError && error.status === 403) {
      clear();
      onPermissionRevoked(error);
      return false;
    }
    lane = {
      ...lane,
      phase: lane.branding ? "ready" : "error",
      busy: false,
      error: fixedError(error),
      operation: null,
    };
    emit();
    return false;
  }

  function resetLane() {
    lane.operation?.controller.abort();
    selectedDeploymentId = "";
    workspaceId = "";
    lane = emptyLane();
  }

  function clear() {
    ownerKey = "";
    canRead = false;
    canManage = false;
    deployments = new Map();
    resetLane();
    emit();
  }

  function snapshot() {
    return Object.freeze({
      selectedDeploymentId,
      canRead,
      canManage,
      phase: lane.phase,
      branding: lane.branding,
      error: lane.error,
      feedback: lane.feedback,
      busy: lane.busy,
    });
  }

  function emit() { onChange(snapshot()); }

  return Object.freeze({ enter, select, close, load, mutate, clear, snapshot });
}

function ownerKeyFor(owner) {
  if (!owner?.readIdentity || !owner.navigationOwner) return "";
  return JSON.stringify([
    owner.readIdentity,
    owner.navigationOwner,
    owner.brandingReadIdentity ?? "",
    owner.brandingManageIdentity ?? "",
  ]);
}

function emptyLane() { return { phase: "idle", branding: null, error: null, feedback: null, busy: false, generation: 0, operation: null, intent: null }; }
function accepted(previous, branding, feedback) { return { ...previous, phase: "ready", branding, error: null, feedback, busy: false, operation: null, intent: null }; }

async function commandFingerprint(brandId, command, method) {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new TypeError("secure deployment brand intent hashing unavailable");
  const bytes = new TextEncoder().encode(JSON.stringify([brandId, command, method]));
  const digest = new Uint8Array(await subtle.digest("SHA-256", bytes));
  return [...digest].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function actionMessage(action) {
  return ({ update: "Gateway 品牌草稿已保存", attach_asset: "PNG 已进入扫描队列", remove_asset: "品牌资产已移除", publish: "品牌包已发布", suspend: "品牌已暂停", rollback_lkg: "已回滚至本地 LKG" })[action] ?? "品牌命令已完成";
}

function fixedError(error) {
  return Object.freeze({
    code: /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(error?.code ?? "")) ? error.code : "branding.unavailable",
    requestId: /^[A-Za-z0-9_.:-]{1,160}$/u.test(String(error?.requestId ?? "")) ? error.requestId : "-",
  });
}
