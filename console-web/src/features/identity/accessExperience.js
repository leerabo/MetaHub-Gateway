import { icon } from "../public-home/icons.js";

export function installIdentityAccessExperience(root, {
  documentObject = globalThis.document,
  loadScene = () => import("../public-home/gatewayScene.js"),
} = {}) {
  const view = documentObject.defaultView ?? globalThis;
  const host = documentObject.querySelector("[data-access-scene]");
  const toggle = documentObject.querySelector("[data-access-motion]");
  const reduced = view.matchMedia("(prefers-reduced-motion: reduce)");
  let scene;
  let loading = false;
  let disposed = false;
  let scheduled = null;
  let generation = 0;
  function eligible() {
    if (disposed || !host || !toggle || reduced.matches || documentObject.hidden
      || !root.querySelector(".identity-login-form")) return false;
    const bounds = host.getBoundingClientRect();
    return bounds.width > 0 && bounds.height > 0;
  }
  function stopScene() {
    generation += 1;
    if (scheduled) {
      if (scheduled.idle) view.cancelIdleCallback(scheduled.id);
      else view.clearTimeout(scheduled.id);
      scheduled = null;
    }
    loading = false;
    scene?.destroy();
    scene = null;
    if (toggle) toggle.hidden = true;
  }
  function sync() {
    const access = Boolean(root.querySelector(".identity-login-shell"));
    documentObject.body.classList.toggle("identity-is-access", access);
    if (!access || !eligible()) {
      stopScene();
      return;
    }
    if (loading || scene || scheduled) return;
    const ticket = ++generation;
    // The account form paints before decorative imports and WebGL initialization.
    const start = () => {
      scheduled = null;
      if (ticket !== generation || !eligible()) return;
      loading = true;
      void loadScene().then(({ createGatewayScene }) => {
        if (ticket !== generation || !eligible()) return;
        scene = createGatewayScene(host, { onMotionChange(paused) {
          toggle.setAttribute("aria-pressed", String(paused));
          toggle.setAttribute("aria-label", paused ? "播放动画" : "暂停动画");
          toggle.title = paused ? "播放动画" : "暂停动画";
          toggle.innerHTML = icon(paused ? "play" : "pause");
        } });
        toggle.hidden = false;
      }).catch(() => {
        if (ticket === generation) toggle.hidden = true;
      }).finally(() => {
        if (ticket === generation) loading = false;
      });
    };
    const idle = typeof view.requestIdleCallback === "function";
    scheduled = { idle, id: idle ? view.requestIdleCallback(start) : view.setTimeout(start, 0) };
  }
  function toggleMotion() { scene?.setPaused(!scene.paused); }
  function togglePassword(event) {
    const button = event.target.closest?.("[data-password-toggle]");
    if (!button || !root.contains(button)) return;
    const input = button.parentElement.querySelector("input");
    const revealed = input.type === "password";
    input.type = revealed ? "text" : "password";
    button.setAttribute("aria-pressed", String(revealed));
    button.setAttribute("aria-label", revealed ? "隐藏密码" : "显示密码");
    button.title = revealed ? "隐藏密码" : "显示密码";
    button.innerHTML = icon(revealed ? "eye-off" : "eye");
  }
  const observer = new view.MutationObserver(sync);
  observer.observe(root, { childList: true });
  const resize = new view.ResizeObserver(sync);
  if (host) resize.observe(host);
  documentObject.addEventListener("visibilitychange", sync);
  reduced.addEventListener("change", sync);
  toggle?.addEventListener("click", toggleMotion);
  root.addEventListener("click", togglePassword);
  sync();
  return () => {
    disposed = true;
    observer.disconnect();
    resize.disconnect();
    documentObject.removeEventListener("visibilitychange", sync);
    reduced.removeEventListener("change", sync);
    stopScene();
    toggle?.removeEventListener("click", toggleMotion);
    root.removeEventListener("click", togglePassword);
  };
}
