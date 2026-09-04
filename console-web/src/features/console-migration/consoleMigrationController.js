/** PRD-147 controller. Step-up material is scrubbed before every return path. */

import { PlatformApiError } from "../../lib/platform.js";
import { createConsoleMigrationStore } from "./consoleMigrationStore.js";

export function createConsoleMigrationController({
  api,
  newIdempotencyKey = defaultIdempotencyKey,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
} = {}) {
  if (!api) throw new TypeError("console migration controller requires api");
  const store = createConsoleMigrationStore({ onChange });
  let context = null;

  function enter(nextContext, navigation) {
    const accepted = store.enter(nextContext, navigation);
    context = accepted ? nextContext : null;
    return accepted;
  }

  async function load() {
    if (!permissions().has("platform.console_migration.read")) return false;
    const ticket = store.begin("load");
    if (!ticket) return false;
    try {
      const value = await api.consoleMigrationStatus({
        ...ticket.filters,
        cursor: ticket.cursor,
        limit: 50,
        signal: ticket.signal,
      });
      return store.acceptLoad(ticket, value);
    } catch (error) {
      return handle(ticket, error);
    }
  }

  async function configure(filters) {
    if (!store.configure(filters)) return false;
    return load();
  }

  async function move(direction) {
    if (!store.move(direction)) return false;
    return load();
  }

  async function command(kind, commandValue, stepUp = {}) {
    const permission = commandPermission(kind, commandValue?.action);
    if (!permission || !permissions().has(permission)) return false;
    const fingerprint = JSON.stringify([kind, commandValue]);
    const intent = store.commandIntent(
      fingerprint,
      () => newIdempotencyKey(`console-migration-${kind}`),
    );
    if (!intent) return false;
    const ticket = store.begin("command", { kind });
    if (!ticket) return false;
    let verificationCode = String(stepUp?.verificationCode ?? "");
    let credential = null;
    let grant = "";
    try {
      const method = String(stepUp?.method ?? "totp");
      let challengeId;
      if (method === "webauthn") {
        const challenge = await api.beginIdentityStepUp({ signal: ticket.signal });
        if (!store.isCurrent(ticket)) return false;
        challengeId = challenge.challenge_id;
        credential = await api.getWebAuthnAssertion(challenge.public_key_options);
      } else if (method !== "totp") return false;
      const resourceId = String(
        commandValue?.migration_id ?? commandValue?.domain_code ?? "console-migration",
      );
      const stepUpResult = await api.createIdentityStepUpGrant({
        method,
        actionCode: `console_migration.${kind}.${String(commandValue?.action ?? "")}`,
        resourceType: "console_domain_migration",
        resourceId,
        code: verificationCode,
        challengeId,
        credential,
        signal: ticket.signal,
      });
      verificationCode = "";
      credential = null;
      grant = String(stepUpResult.grant ?? "");
      if (!grant || !store.isCurrent(ticket)) return false;
      const invoke = kind === "backfill" ? api.consoleMigrationBackfill
        : kind === "cutover" ? api.consoleMigrationCutover
          : kind === "rollback" ? api.consoleMigrationRollback : null;
      if (typeof invoke !== "function") return false;
      const value = await invoke({
        command: commandValue,
        expectedRevision: commandValue.expected_revision,
        idempotencyKey: intent.idempotencyKey,
        stepUpGrant: grant,
        signal: ticket.signal,
      });
      grant = "";
      if (!store.acceptCommand(ticket, value)) return false;
      await load();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        store.clearIntent();
      }
      return handle(ticket, error);
    } finally {
      verificationCode = "";
      credential = null;
      grant = "";
    }
  }

  function leave() {
    context = null;
    store.leave();
  }

  function handle(ticket, error) {
    if (!store.reject(ticket, error)) return false;
    if (error instanceof PlatformApiError && error.status === 401) {
      leave();
      onSessionInvalid(error);
    } else if (error instanceof PlatformApiError && error.status === 403) {
      onPermissionRevoked(error);
    }
    return false;
  }

  function permissions() { return new Set(context?.permissions ?? []); }

  return Object.freeze({
    enter,
    load,
    configure,
    move,
    command,
    leave,
    snapshot: store.snapshot,
  });
}

function commandPermission(kind, action) {
  if (kind === "backfill") return "platform.console_migration.backfill";
  if (action === "approve") return "platform.console_migration.approve";
  if (action === "security_review") return "platform.console_migration.security_review";
  if (action === "execute") return "platform.console_migration.execute";
  if (kind === "cutover" || kind === "rollback") return "platform.console_migration.backfill";
  return "";
}

function defaultIdempotencyKey(kind) {
  const crypto = globalThis.crypto;
  if (!crypto || typeof crypto.getRandomValues !== "function") {
    throw new PlatformApiError({
      code: "platform.configuration_invalid",
      message: "当前环境无法生成安全请求标识。",
    });
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const suffix = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${kind}:${suffix}`;
}
