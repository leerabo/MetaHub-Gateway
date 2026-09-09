import { icon } from "../public-home/icons.js";

export function installIdentityAccessExperience(root) {
  const host = document.querySelector("[data-access-scene]");
  const toggle = document.querySelector("[data-access-motion]");
  let scene;
  let loading = false;
  let disposed = false;
  function sync() {
    const access = Boolean(root.querySelector(".identity-login-shell"));
    document.body.classList.toggle("identity-is-access", access);
    if (!access) {
      scene?.destroy();
      scene = null;
      if (toggle) toggle.hidden = true;
      return;
    }
    if (!host || loading || scene || disposed) return;
    loading = true;
    void import("../public-home/gatewayScene.js").then(({ createGatewayScene }) => {
      if (disposed || !root.querySelector(".identity-login-shell")) return;
      scene = createGatewayScene(host, { onMotionChange(paused) {
        toggle.setAttribute("aria-pressed", String(paused));
        toggle.setAttribute("aria-label", paused ? "播放动画" : "暂停动画");
        toggle.title = paused ? "播放动画" : "暂停动画";
        toggle.innerHTML = icon(paused ? "play" : "pause");
      } });
      toggle.hidden = false;
    }).catch(() => { toggle.hidden = true; }).finally(() => { loading = false; });
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
  const observer = new MutationObserver(sync);
  observer.observe(root, { childList: true });
  toggle?.addEventListener("click", toggleMotion);
  root.addEventListener("click", togglePassword);
  sync();
  return () => {
    disposed = true;
    observer.disconnect();
    scene?.destroy();
    toggle?.removeEventListener("click", toggleMotion);
    root.removeEventListener("click", togglePassword);
  };
}
