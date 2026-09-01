/** Platform and Channel brand owner state. The two lanes never share projections or commands. */

import { PlatformApiError } from "../../lib/platform.js";

export function createPlatformBrandingController({
  api,
  newIdempotencyKey,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
} = {}) {
  if (!api || typeof newIdempotencyKey !== "function") throw new TypeError("branding controller requires api");
  let ownerKey = "";
  let permissions = new Set();
  let platform = emptyLane();
  let channel = emptyChannelLane();

  function enter(context, navigation) {
    const nextKey = platformOwnerKey(context);
    const ids = new Set((navigation ?? []).map((item) => item?.id));
    if (!nextKey) { clear(); return false; }
    if (ownerKey && ownerKey !== nextKey) clear();
    ownerKey = nextKey;
    permissions = new Set(context?.permissions ?? []);
    if (!ids.has("branding") || !permissions.has("platform.brand.read")) resetPlatform();
    if (!ids.has("channels") || !permissions.has("platform.channel_brand.read")) resetChannel();
    emit();
    return true;
  }

  async function loadPlatform() {
    if (!ownerKey || !permissions.has("platform.brand.read") || typeof api.platformBrand !== "function") return false;
    const ticket = beginPlatform("loading");
    emit();
    try {
      const response = await api.platformBrand({ signal: ticket.controller.signal });
      if (!currentPlatform(ticket)) return false;
      platform = acceptedLane(platform, response.branding, null);
      emit();
      return true;
    } catch (error) {
      return reject(error, ticket, "platform");
    }
  }

  async function selectChannel(partnerId) {
    const id = String(partnerId ?? "");
    if (!/^par_[0-9a-f]{32}$/u.test(id) || !permissions.has("platform.channel_brand.read")) return false;
    if (channel.partnerId !== id) resetChannel();
    channel = { ...channel, partnerId: id };
    emit();
    return loadChannel();
  }

  async function loadChannel() {
    if (!ownerKey || !channel.partnerId || !permissions.has("platform.channel_brand.read")
      || typeof api.channelBrand !== "function") return false;
    const ticket = beginChannel("loading");
    emit();
    try {
      const response = await api.channelBrand({
        partnerId: channel.partnerId, signal: ticket.controller.signal,
      });
      if (!currentChannel(ticket)) return false;
      channel = acceptedChannel(channel, response.branding, null);
      emit();
      return true;
    } catch (error) {
      return reject(error, ticket, "channel");
    }
  }

  async function mutatePlatform(input) {
    const current = platform.branding;
    if (!current || !current.actions.includes(input?.command?.action)
      || typeof api.updatePlatformBrand !== "function") return false;
    const fingerprint = await commandFingerprint("platform", current.platform_brand_id, input.command, input.method);
    const idempotencyKey = platform.intent?.fingerprint === fingerprint
      ? platform.intent.idempotencyKey : newIdempotencyKey("platform-brand");
    platform = { ...platform, intent: { fingerprint, idempotencyKey } };
    return mutate({ lane: "platform", current, input, idempotencyKey, invoke: (stepUpGrant, signal) => api.updatePlatformBrand({
      command: input.command,
      expectedRevision: current.revision,
      stepUpGrant,
      idempotencyKey,
      signal,
    }) });
  }

  async function mutateChannel(input) {
    const current = channel.branding;
    if (!current || !channel.partnerId || !current.actions.includes(input?.command?.action)
      || typeof api.updateChannelBrand !== "function") return false;
    const fingerprint = await commandFingerprint("channel", current.channel_brand_id, input.command, input.method);
    const idempotencyKey = channel.intent?.fingerprint === fingerprint
      ? channel.intent.idempotencyKey : newIdempotencyKey("channel-brand");
    channel = { ...channel, intent: { fingerprint, idempotencyKey } };
    return mutate({ lane: "channel", current, input, idempotencyKey, invoke: (stepUpGrant, signal) => api.updateChannelBrand({
      partnerId: channel.partnerId,
      command: input.command,
      expectedRevision: current.revision,
      stepUpGrant,
      idempotencyKey,
      signal,
    }) });
  }

  async function mutate({ lane, current, input, invoke }) {
    if (input?.confirmed !== true || !new Set(["totp", "webauthn"]).has(input?.method)) return false;
    const ticket = lane === "platform" ? beginPlatform("ready", true) : beginChannel("ready", true);
    emit();
    let verificationCode = String(input.verificationCode ?? "");
    let stepUpGrant = "";
    try {
      const kind = lane === "platform" ? "platform" : "channel";
      const brandId = lane === "platform" ? current.platform_brand_id : current.channel_brand_id;
      stepUpGrant = await platformStepUp({
        method: input.method,
        verificationCode,
        action: input.command.action,
        kind,
        brandId,
        ticket,
        lane,
      });
      verificationCode = "";
      if (!stepUpGrant || !(lane === "platform" ? currentPlatform(ticket) : currentChannel(ticket))) return false;
      const response = await invoke(stepUpGrant, ticket.controller.signal);
      stepUpGrant = "";
      if (!(lane === "platform" ? currentPlatform(ticket) : currentChannel(ticket))) return false;
      const feedback = Object.freeze({ message: actionMessage(input.command.action), requestId: response.request_id });
      if (lane === "platform") platform = acceptedLane(platform, response.branding, feedback);
      else channel = acceptedChannel(channel, response.branding, feedback);
      emit();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        if (lane === "platform") platform = { ...platform, intent: null };
        else channel = { ...channel, intent: null };
      }
      return reject(error, ticket, lane);
    } finally {
      verificationCode = "";
      stepUpGrant = "";
    }
  }

  async function platformStepUp({ method, verificationCode, action, kind, brandId, ticket, lane }) {
    let challengeId;
    let credential = null;
    const current = () => lane === "platform" ? currentPlatform(ticket) : currentChannel(ticket);
    if (method === "webauthn") {
      const challenge = await api.beginIdentityStepUp({ signal: ticket.controller.signal });
      if (!current()) return "";
      challengeId = challenge.challenge_id;
      credential = await api.getWebAuthnAssertion(challenge.public_key_options);
      if (!current()) return "";
    }
    const response = await api.createIdentityStepUpGrant({
      method,
      actionCode: `brand.${kind}.${action}`,
      resourceType: `${kind}_brand`,
      resourceId: brandId,
      code: verificationCode,
      challengeId,
      credential,
      signal: ticket.controller.signal,
    });
    credential = null;
    return current() ? String(response.grant ?? "") : "";
  }

  function beginPlatform(phase, busy = false) {
    platform.operation?.controller.abort();
    const ticket = ticketFor("platform", platform.generation + 1, ownerKey);
    platform = { ...platform, phase, busy, error: null, feedback: null, generation: ticket.generation, operation: ticket };
    return ticket;
  }

  function beginChannel(phase, busy = false) {
    channel.operation?.controller.abort();
    const ticket = ticketFor("channel", channel.generation + 1, ownerKey, channel.partnerId);
    channel = { ...channel, phase, busy, error: null, feedback: null, generation: ticket.generation, operation: ticket };
    return ticket;
  }

  function currentPlatform(ticket) {
    return Boolean(ticket && platform.operation === ticket && ticket.ownerKey === ownerKey
      && ticket.generation === platform.generation && !ticket.controller.signal.aborted);
  }

  function currentChannel(ticket) {
    return Boolean(ticket && channel.operation === ticket && ticket.ownerKey === ownerKey
      && ticket.partnerId === channel.partnerId && ticket.generation === channel.generation
      && !ticket.controller.signal.aborted);
  }

  function reject(error, ticket, lane) {
    const current = lane === "platform" ? currentPlatform(ticket) : currentChannel(ticket);
    if (!current || error?.name === "AbortError") return false;
    if (error instanceof PlatformApiError && error.status === 401) {
      clear();
      onSessionInvalid(error);
      return false;
    }
    if (error instanceof PlatformApiError && error.status === 403) {
      if (lane === "platform") resetPlatform(); else resetChannel();
      emit();
      onPermissionRevoked(error);
      return false;
    }
    const fixed = fixedError(error);
    if (lane === "platform") platform = { ...platform, phase: platform.branding ? "ready" : "error", busy: false, error: fixed, operation: null };
    else channel = { ...channel, phase: channel.branding ? "ready" : "error", busy: false, error: fixed, operation: null };
    emit();
    return false;
  }

  function leavePlatform() { resetPlatform(); emit(); }
  function leaveChannels() { resetChannel(); emit(); }
  function clear() { ownerKey = ""; permissions = new Set(); resetPlatform(); resetChannel(); emit(); }
  function resetPlatform() { platform.operation?.controller.abort(); platform = emptyLane(); }
  function resetChannel() { channel.operation?.controller.abort(); channel = emptyChannelLane(); }
  function snapshot() { return Object.freeze({ platform: laneSnapshot(platform), channel: channelSnapshot(channel) }); }
  function emit() { onChange(snapshot()); }

  return Object.freeze({
    enter, loadPlatform, selectChannel, loadChannel, mutatePlatform, mutateChannel,
    leavePlatform, leaveChannels, clear, snapshot,
  });
}

function emptyLane() { return { phase: "idle", branding: null, error: null, feedback: null, busy: false, generation: 0, operation: null, intent: null }; }
function emptyChannelLane() { return { ...emptyLane(), partnerId: "" }; }
function acceptedLane(previous, branding, feedback) { return { ...previous, phase: "ready", branding, error: null, feedback, busy: false, operation: null, intent: null }; }
function acceptedChannel(previous, branding, feedback) { return acceptedLane(previous, branding, feedback); }
function laneSnapshot(value) { return Object.freeze({ phase: value.phase, branding: value.branding, error: value.error, feedback: value.feedback, busy: value.busy }); }
function channelSnapshot(value) { return Object.freeze({ ...laneSnapshot(value), partnerId: value.partnerId }); }
function ticketFor(lane, generation, ownerKey, partnerId = "") { return Object.freeze({ lane, generation, partnerId, ownerKey, controller: new AbortController() }); }

function platformOwnerKey(context) {
  const operator = context?.operator;
  const id = String(operator?.operator_id ?? "");
  if (!/^pto_[0-9a-f]{32}$/u.test(id) || operator?.status !== "active") return "";
  return JSON.stringify([id, Number(operator.revision ?? 0), [...(operator.roles ?? [])].sort(), [...(context?.permissions ?? [])].sort(), String(context?.expires_at ?? "")]);
}

async function commandFingerprint(kind, brandId, command, method) {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new TypeError("secure brand intent hashing unavailable");
  const input = new TextEncoder().encode(JSON.stringify([kind, brandId, command, method]));
  const digest = new Uint8Array(await subtle.digest("SHA-256", input));
  return [...digest].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function actionMessage(action) {
  return ({ update: "品牌草稿已保存", attach_asset: "PNG 已进入扫描队列", remove_asset: "品牌资产已移除", request_domain: "域名验证已创建", release: "公开品牌已发布", publish: "Gateway 品牌已发布", suspend: "品牌已暂停", rollback_lkg: "已回滚至 LKG" })[action] ?? "品牌命令已完成";
}

function fixedError(error) {
  return Object.freeze({
    code: /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(error?.code ?? "")) ? error.code : "branding.unavailable",
    requestId: /^[A-Za-z0-9_.:-]{1,160}$/u.test(String(error?.requestId ?? "")) ? error.requestId : "-",
  });
}
