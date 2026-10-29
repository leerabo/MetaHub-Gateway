import { createPlatformConsoleExperienceStore } from "./platformConsoleExperienceStore.js";

const VIEW_PERMISSION = Object.freeze({
  overview: "platform.console.overview.read",
  "admin-assistant": "platform.console.assistant.query",
  approvals: "platform.console.approval.read",
});

export function createPlatformConsoleExperienceController({
  api,
  onChange = () => {},
  onSessionInvalid = () => {},
  onPermissionRevoked = () => {},
} = {}) {
  if (!api) throw new TypeError("platform console experience requires api");
  const store = createPlatformConsoleExperienceStore();
  let owner = null;

  function enter(context, navigation, view) {
    owner = platformConsoleExperienceOwner(context, navigation, view);
    const accepted = store.enter(owner, view);
    publish();
    return accepted;
  }

  async function load() {
    const view = store.snapshot().view;
    if (view === "admin-assistant") return true;
    const ticket = store.begin(view);
    if (!ticket) return false;
    publish();
    try {
      const value = view === "overview"
        ? await api.consoleOverview({ signal: ticket.signal })
        : await api.consoleApprovals({
          ...ticket.filters,
          cursor: ticket.cursor,
          signal: ticket.signal,
        });
      const accepted = store.accept(ticket, value, owner);
      publish();
      return accepted;
    } catch (error) {
      return handle(ticket, error);
    }
  }

  function setAssistantDraft(value) {
    const changed = store.setAssistantDraft(owner, value);
    if (changed) publish();
    return changed;
  }

  async function submitAssistant() {
    const ticket = store.begin("assistant");
    if (!ticket) return false;
    publish();
    try {
      const value = await api.consoleAssistant({
        question: ticket.question,
        signal: ticket.signal,
      });
      const accepted = store.accept(ticket, value, owner);
      publish();
      return accepted;
    } catch (error) {
      return handle(ticket, error);
    }
  }

  function clearAssistant() {
    const changed = store.clearAssistant(owner);
    if (changed) publish();
    return changed;
  }

  async function configureApprovals(filters) {
    if (!store.configureApprovals(owner, filters)) return false;
    publish();
    return load();
  }

  async function nextApprovals() {
    if (!store.nextApprovals(owner)) return false;
    publish();
    return load();
  }

  async function previousApprovals() {
    if (!store.previousApprovals(owner)) return false;
    publish();
    return load();
  }

  function leave() {
    owner = null;
    store.leave();
    publish();
  }

  function snapshot() {
    return store.snapshot();
  }

  async function handle(ticket, error) {
    const accepted = store.reject(ticket, error, owner);
    publish();
    if (accepted && error?.status === 401) onSessionInvalid(error);
    else if (accepted && error?.status === 403) onPermissionRevoked(error);
    return false;
  }

  function publish() {
    onChange(store.snapshot());
  }

  return Object.freeze({
    enter,
    load,
    setAssistantDraft,
    submitAssistant,
    clearAssistant,
    configureApprovals,
    nextApprovals,
    previousApprovals,
    leave,
    snapshot,
  });
}

export function platformConsoleExperienceOwner(context, navigation, view) {
  const permission = VIEW_PERMISSION[view];
  const permissions = Array.isArray(context?.permissions) ? context.permissions.map(String) : [];
  const item = Array.isArray(navigation)
    ? navigation.find((candidate) => candidate?.id === view)
    : null;
  const operatorId = String(context?.operator?.operator_id ?? "");
  const sessionRevision = Number(context?.operator?.revision);
  const roles = Array.isArray(context?.operator?.roles) ? context.operator.roles.map(String) : [];
  if (!permission || !permissions.includes(permission) || !item || !operatorId
    || !Number.isSafeInteger(sessionRevision) || sessionRevision < 1 || roles.length === 0) return null;
  return Object.freeze({
    operatorId,
    sessionRevision,
    roles: Object.freeze([...roles]),
    permissions: Object.freeze([...permissions]),
  });
}
