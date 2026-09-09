import { icon } from "./icons.js";
import { legacyConsoleTarget } from "./entryRoute.js";

const redirect = legacyConsoleTarget(window.location);
if (redirect) {
  window.location.replace(redirect);
} else {
  for (const element of document.querySelectorAll("[data-icon]")) element.innerHTML = icon(element.dataset.icon);
  const host = document.querySelector("[data-gateway-scene]");
  const toggle = document.querySelector("[data-motion-toggle]");
  let scene;
  let disposed = false;
  function updateMotion(paused) {
    toggle.setAttribute("aria-pressed", String(paused));
    toggle.setAttribute("aria-label", paused ? "播放动画" : "暂停动画");
    toggle.title = paused ? "播放动画" : "暂停动画";
    toggle.innerHTML = icon(paused ? "play" : "pause");
  }
  void import("./gatewayScene.js").then(({ createGatewayScene }) => {
    if (disposed) return;
    scene = createGatewayScene(host, { onMotionChange: updateMotion });
    toggle.hidden = false;
    toggle.addEventListener("click", () => scene.setPaused(!scene.paused));
  }).catch(() => {
    toggle.hidden = true;
  });
  window.addEventListener("hashchange", () => {
    const target = legacyConsoleTarget(window.location);
    if (target) window.location.replace(target);
  });
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    disposed = true;
    scene?.destroy();
  });
}
