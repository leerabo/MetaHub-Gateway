import { createAccountPreferenceStore } from "./accountPreferenceStore.js";

export function createAccountPreferenceController({
  api,
  createIdempotencyKey,
  onChange = () => {},
  onSessionInvalid = () => {},
} = {}) {
  if (!api) throw new TypeError("account preference controller requires api");
  const store = createAccountPreferenceStore({ createIdempotencyKey });
  let owner = null;

  function enter(nextOwner) {
    owner = nextOwner;
    const accepted = store.enter(owner);
    publish();
    return accepted;
  }

  async function load() {
    const ticket = store.beginLoad(owner);
    if (!ticket) return false;
    publish();
    try {
      const value = await api.preferences({ signal: ticket.signal });
      const accepted = store.accept(ticket, value, owner);
      publish();
      return accepted;
    } catch (error) {
      return handle(ticket, error);
    }
  }

  async function save(locale) {
    const ticket = store.beginSave(owner, locale);
    if (!ticket) return false;
    publish();
    try {
      const value = await api.updatePreferences({
        locale: ticket.locale,
        revision: ticket.revision,
        idempotencyKey: ticket.idempotencyKey,
        signal: ticket.signal,
      });
      const accepted = store.accept(ticket, value, owner);
      publish();
      return accepted;
    } catch (error) {
      return handle(ticket, error);
    }
  }

  function leave() {
    owner = null;
    store.leave();
    publish();
  }

  function snapshot() {
    return store.snapshot();
  }

  function handle(ticket, error) {
    const accepted = store.reject(ticket, error, owner);
    publish();
    if (accepted && error?.status === 401) onSessionInvalid(error);
    return false;
  }

  function publish() {
    onChange(store.snapshot());
  }

  return Object.freeze({ enter, load, save, leave, snapshot });
}
