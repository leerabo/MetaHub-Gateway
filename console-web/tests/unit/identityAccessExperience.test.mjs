import assert from "node:assert/strict";
import test from "node:test";
import { installIdentityAccessExperience } from "../../src/features/identity/accessExperience.js";

function fixture({ form = true, visible = true, reduced = false, fallback = false, loadScene } = {}) {
  const idle = new Map();
  const observations = [];
  const facts = { loaded: 0, created: 0, destroyed: 0 };
  const eventTarget = () => ({
    listeners: new Map(),
    addEventListener(name, fn) { this.listeners.set(name, fn); },
    removeEventListener(name) { this.listeners.delete(name); },
    dispatch(name) { this.listeners.get(name)?.(); },
  });
  class Observer {
    constructor(callback) { this.callback = callback; observations.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
  }
  const media = { ...eventTarget(), matches: reduced };
  let next = 0;
  const schedule = (callback) => { idle.set(++next, callback); return next; };
  const view = {
    matchMedia: () => media, MutationObserver: Observer, ResizeObserver: Observer,
    requestIdleCallback: fallback ? undefined : schedule,
    cancelIdleCallback: (id) => idle.delete(id),
    setTimeout: schedule, clearTimeout: (id) => idle.delete(id),
  };
  const host = { visible, getBoundingClientRect() { return { width: this.visible ? 600 : 0, height: this.visible ? 300 : 0 }; } };
  const toggle = { ...eventTarget(), hidden: true, setAttribute() {} };
  const root = { ...eventTarget(), form, access: true, querySelector(selector) {
    return (selector === ".identity-login-form" ? this.form : this.access) ? {} : null;
  } };
  const documentObject = {
    ...eventTarget(), defaultView: view, hidden: false, body: { classList: { toggle() {} } },
    querySelector: (selector) => selector === "[data-access-scene]" ? host : toggle,
  };
  const sceneModule = { createGatewayScene() {
    facts.created += 1;
    return { destroy() { facts.destroyed += 1; } };
  } };
  const dispose = installIdentityAccessExperience(root, {
    documentObject, loadScene: () => { facts.loaded += 1; return loadScene ? loadScene(sceneModule) : Promise.resolve(sceneModule); },
  });
  return {
    facts, root, host, toggle, media, documentObject, dispose, idle, observations,
    sync: () => observations[0].callback(),
    resize: () => observations[1].callback(),
    async flush() {
      for (const [id, callback] of [...idle]) { idle.delete(id); callback(); }
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    },
  };
}

test("the loading skeleton does not import WebGL; a visible form starts only when idle", async () => {
  const state = fixture({ form: false });
  assert.equal(state.idle.size, 0);
  state.root.form = true;
  state.sync();
  assert.equal(state.facts.loaded, 0);
  assert.equal(state.idle.size, 1);
  await state.flush();
  assert.deepEqual(state.facts, { loaded: 1, created: 1, destroyed: 0 });
  assert.equal(state.toggle.hidden, false);
  state.dispose();
});

test("hidden mobile layout never creates a context and resize can restore a visible scene", async () => {
  const state = fixture({ visible: false });
  await state.flush();
  assert.equal(state.facts.loaded, 0);
  state.host.visible = true;
  state.resize();
  await state.flush();
  assert.equal(state.facts.created, 1);
  state.host.visible = false;
  state.resize();
  assert.equal(state.facts.destroyed, 1);
  assert.equal(state.toggle.hidden, true);
  state.dispose();
});

test("reduced motion keeps the static visual and preference changes restore animation", async () => {
  const state = fixture({ reduced: true });
  await state.flush();
  assert.equal(state.facts.loaded, 0);
  state.media.matches = false;
  state.media.dispatch("change");
  await state.flush();
  assert.equal(state.facts.created, 1);
  state.media.matches = true;
  state.media.dispatch("change");
  assert.equal(state.facts.destroyed, 1);
  state.dispose();
});

test("leaving the form cancels scheduled work and rejects a late imported scene", async () => {
  let resolve;
  const state = fixture({ loadScene: (module) => new Promise((done) => { resolve = () => done(module); }) });
  state.root.form = false;
  state.sync();
  await state.flush();
  assert.equal(state.facts.loaded, 0);
  state.root.form = true;
  state.sync();
  await state.flush();
  state.root.access = false;
  state.root.form = false;
  state.sync();
  resolve();
  await state.flush();
  assert.equal(state.facts.created, 0);
  assert.equal(state.toggle.hidden, true);
  state.dispose();
});

test("hidden documents release the context and teardown cancels fallback work", async () => {
  const state = fixture({ fallback: true });
  await state.flush();
  assert.equal(state.facts.created, 1);
  state.documentObject.hidden = true;
  state.documentObject.dispatch("visibilitychange");
  assert.equal(state.facts.destroyed, 1);
  state.documentObject.hidden = false;
  state.documentObject.dispatch("visibilitychange");
  assert.equal(state.idle.size, 1);
  state.dispose();
  await state.flush();
  assert.equal(state.facts.created, 1);
  assert.equal(state.media.listeners.size, 0);
  assert.equal(state.documentObject.listeners.size, 0);
  assert.ok(state.observations.every((observer) => observer.disconnected));
});

test("a scene import failure leaves form interactions installed", async () => {
  const state = fixture({ loadScene: async () => { throw new Error("unavailable"); } });
  await state.flush();
  assert.equal(state.toggle.hidden, true);
  assert.equal(state.facts.created, 0);
  assert.ok(state.root.listeners.has("click"));
  state.dispose();
  assert.equal(state.root.listeners.size, 0);
});
